import logging
import time

import boto3
from botocore.exceptions import ClientError, WaiterError

from sagemaker_ssh_helper.log import SSHLog
from sagemaker_ssh_helper.manager import SSMManager


class IDEAppStatus:

    def __init__(self, status, failure_reason=None) -> None:
        super().__init__()
        self.failure_reason = failure_reason
        self.status = status

    def is_pending(self):
        return self.status == 'Pending'

    def is_in_transition(self):
        return self.status == 'Deleting' or self.status == 'Pending'

    def is_deleting(self):
        return self.status == 'Deleting'

    def is_in_service(self):
        return self.status == 'InService'

    def is_deleted(self):
        return self.status == 'Deleted'

    def __str__(self) -> str:
        if self.failure_reason:
            return f"{self.status}, failure reason: {self.failure_reason}"
        return f"{self.status}"


class Image:
    def __init__(self, arn, version_arn) -> None:
        super().__init__()
        self.arn = arn
        self.version_arn = version_arn


class SSHIDE:
    logger = logging.getLogger('sagemaker-ssh-helper:SSHIDE')

    def __init__(self, domain_id: str, user_or_space: str = None, region_name: str = None, is_user_profile: bool = True):
        self.user_or_space = user_or_space
        self.domain_id = domain_id
        self.current_region = region_name or boto3.session.Session().region_name
        self.client = boto3.client('sagemaker', region_name=self.current_region)
        self.ssh_log = SSHLog(region_name=self.current_region)
        self.is_user_profile = is_user_profile

    def create_ssh_kernel_app(self, app_name: str,
                              image_name_or_arn='sagemaker-datascience-38',
                              instance_type='ml.m5.xlarge',
                              ssh_lifecycle_config='sagemaker-ssh-helper',
                              recreate=False):
        """
        Creates new kernel app with SSH lifecycle config (see kernel-lc-config.sh ).

        Images: https://docs.aws.amazon.com/sagemaker/latest/dg/notebooks-available-images.html .

        Note that doc is not always up-to-date and doesn't list full names,
          e.g., sagemaker-base-python-310 in the doc is sagemaker-base-python-310-v1 in the CreateApp API .

        :param app_name:
        :param image_name_or_arn: [name] from the images doc above or the full ARN
        :param instance_type:
        :param ssh_lifecycle_config:
        :param recreate:
        """
        self.logger.info(f"Creating kernel app {app_name} with SSH lifecycle config {ssh_lifecycle_config}")
        self.log_urls(app_name)
        status = self.get_app_status(app_name)
        while status.is_in_transition():
            self.logger.info(f"Waiting for the final status. Current status: {status}")
            time.sleep(10)
            status = self.get_app_status(app_name)

        self.logger.info(f"Previous app status: {status}")

        if status.is_in_service():
            if recreate:
                self.delete_app(app_name, 'KernelGateway')
            else:
                raise ValueError(f"App {app_name} is in service, pass recreate=True to delete and create again.")

        # Here status is None or 'Deleted' or 'Failed'. Safe to create

        if image_name_or_arn.startswith('arn:'):
            image_arn = image_name_or_arn
        else:
            image_arn = self.resolve_sagemaker_kernel_image_arn(image_name_or_arn)

        account_id = boto3.client('sts').get_caller_identity().get('Account')
        lifecycle_arn = f"arn:aws:sagemaker:{self.current_region}:{account_id}:" \
                        f"studio-lifecycle-config/{ssh_lifecycle_config}"

        self.create_app(app_name, 'KernelGateway', instance_type, image_arn, lifecycle_arn)

    def get_app_status(self, app_name: str, app_type: str = 'KernelGateway') -> IDEAppStatus:
        """
        :param app_type:
        :param app_name:
        :return: None | 'InService' | 'Deleted' | 'Deleting' | 'Failed' | 'Pending'
        """
        response = None

        describe_app_request_params = {
            "DomainId": self.domain_id,
            "AppType": app_type,
            "AppName": app_name,
        }

        describe_app_request_params.update(
            {"UserProfileName": self.user_or_space} if self.is_user_profile else {"SpaceName": self.user_or_space})

        try:
            response = self.client.describe_app(**describe_app_request_params)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == 'ResourceNotFound':
                pass
            else:
                raise

        status = None
        failure_reason = None
        if response:
            status = response['Status']
            if 'FailureReason' in response:
                failure_reason = response['FailureReason']
        return IDEAppStatus(status, failure_reason)

    def delete_kernel_app(self, app_name, wait: bool = True):
        self.delete_app(app_name, 'KernelGateway', wait)

    def delete_app(self, app_name, app_type, wait: bool = True):
        self.logger.info(f"Deleting app {app_name}")

        try:
            delete_app_request_params = {
                "DomainId": self.domain_id,
                "AppType": app_type,
                "AppName": app_name,
            }

            delete_app_request_params.update(
                {"UserProfileName": self.user_or_space} if self.is_user_profile else {"SpaceName": self.user_or_space})

            _ = self.client.delete_app(**delete_app_request_params)
        except ClientError as e:
            # probably, already deleted
            code = e.response.get("Error", {}).get("Code")
            message = e.response.get("Error", {}).get("Message")
            self.logger.warning("ClientError code: " + code)
            self.logger.warning("ClientError message: " + message)
            if code == 'AccessDeniedException':
                raise
            return

        status = self.get_app_status(app_name)
        while wait and status.is_deleting():
            self.logger.info(f"Waiting for the Deleted status. Current status: {status}")
            time.sleep(10)
            status = self.get_app_status(app_name)
        self.logger.info(f"Status after delete: {status}")
        if wait and not status.is_deleted():
            raise ValueError(f"Failed to delete app {app_name}. Status: {status}")

    def create_app(self, app_name, app_type, instance_type, image_arn,
                   lifecycle_arn: str = None):
        self.logger.info(f"Creating {app_type} app {app_name} on {instance_type} "
                         f"with {image_arn} and lifecycle {lifecycle_arn}")
        resource_spec = {
            'InstanceType': instance_type,
            'SageMakerImageArn': image_arn,
        }
        if lifecycle_arn:
            resource_spec['LifecycleConfigArn'] = lifecycle_arn

        create_app_request_params = {
            "DomainId": self.domain_id,
            "AppType": app_type,
            "AppName": app_name,
            "ResourceSpec": resource_spec,
        }

        create_app_request_params.update(
            {"UserProfileName": self.user_or_space} if self.is_user_profile else {"SpaceName": self.user_or_space})

        _ = self.client.create_app(**create_app_request_params)
        status = self.get_app_status(app_name)
        while status.is_pending():
            self.logger.info(f"Waiting for the InService status. Current status: {status}")
            time.sleep(10)
            status = self.get_app_status(app_name)

        self.logger.info(f"New app status: {status}")

        if not status.is_in_service():
            raise ValueError(
                f"Failed to create app {app_name}. Status: '{status}'. "
                f"Check remote logs at {self.get_cloudwatch_url(app_name)} "
                f"for more details, if needed."
            )

    def resolve_sagemaker_kernel_image_arn(self, image_name):
        sagemaker_account_id = "470317259841"  # eu-west-1, TODO: check all images
        return f"arn:aws:sagemaker:{self.current_region}:{sagemaker_account_id}:image/{image_name}"

    def print_instance_id(self, app_name, timeout_in_sec, index: int = 0):
        print(self.get_instance_id(app_name, timeout_in_sec, index))

    def get_instance_id(self, app_name, timeout_in_sec, index: int = 0,
                        not_earlier_than_timestamp: int = 0):
        ids = self.get_instance_ids(app_name, timeout_in_sec, not_earlier_than_timestamp)
        if len(ids) == 0:
            raise ValueError(f"No instances found for app {app_name}")
        return ids[index]

    def get_instance_ids(self, app_name: str, timeout_in_sec: int, not_earlier_than_timestamp: int = 0):
        self.logger.info(f"Resolving IDE instance IDs for app '{app_name}' through SSM tags in domain '{self.domain_id}' "
                         f"for {f'user' if self.is_user_profile else f'space'} '{self.user_or_space}'")
        self.log_urls(app_name)

        if self.domain_id and self.user_or_space:
            result = SSMManager().get_studio_instance_ids(self.domain_id, self.user_or_space, app_name,
                                                          timeout_in_sec, not_earlier_than_timestamp, is_user_profile=self.is_user_profile)
        elif self.user_or_space:
            self.logger.warning(f"Domain ID is not set. Will attempt to connect to the latest "
                                f"active {app_name} in the region {self.current_region} "
                                f"for {'user' if self.is_user_profile else 'space'} {self.user_or_space}")
            result = SSMManager().get_studio_instance_ids("", self.user_or_space, app_name,
                                                          timeout_in_sec, not_earlier_than_timestamp, is_user_profile=self.is_user_profile)
        else:
            self.logger.warning(
                f"Domain ID or {'user' if self.is_user_profile else 'space'} are not set. Will attempt to connect to the latest "
                f"active {app_name} in the region {self.current_region}")
            result = SSMManager().get_studio_kgw_instance_ids(app_name, timeout_in_sec, not_earlier_than_timestamp)
        return result

    def log_urls(self, app_name):
        self.logger.info(f"Remote logs are at {self.get_cloudwatch_url(app_name)}")
        if self.domain_id:
            self.logger.info(f"Remote apps metadata is at {self.get_user_or_space_metadata_url()}")

    def get_cloudwatch_url(self, app_name):
        return self.ssh_log.get_ide_cloudwatch_url(self.domain_id, self.user_or_space, app_name, self.is_user_profile)

    def get_user_or_space_metadata_url(self):
        return self.ssh_log.get_ide_metadata_url(self.domain_id, self.user_or_space, self.is_user_profile)

    def create_and_attach_image(self, image_name, ecr_image_name,
                                role_arn,
                                app_image_config_name,
                                kernel_specs, file_system_config) -> Image:
        try:
            self.client.delete_image(ImageName=image_name)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == 'ResourceNotFound':
                pass  # doesn't exist, it's OK
            else:
                raise
        try:
            self.wait_for_image_deletion(image_name)
        except WaiterError:
            pass  # probably, OK

        sagemaker_image_dict = self.client.create_image(
            ImageName=image_name,
            RoleArn=role_arn
        )
        image_arn = sagemaker_image_dict['ImageArn']
        self.logger.info(f"Creating SageMaker image with ARN: {image_arn}")

        self.wait_for_image_creation(image_name)

        account_id = boto3.client('sts').get_caller_identity().get('Account')
        sagemaker_image_version_dict = self.client.create_image_version(
            BaseImage=f"{account_id}.dkr.ecr.{self.current_region}.amazonaws.com/{ecr_image_name}",
            ImageName=image_name,
            JobType='NOTEBOOK_KERNEL'
        )
        image_version_arn = sagemaker_image_version_dict['ImageVersionArn']
        image_version = int(image_version_arn[image_version_arn.rfind('/') + 1:])
        self.logger.info(f"Creating SageMaker image version # {image_version} with ARN: {image_version_arn}")

        self.wait_for_image_version_creation(image_name)

        try:
            self.client.delete_app_image_config(AppImageConfigName=app_image_config_name)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == 'ResourceNotFound':
                pass  # doesn't exist, it's OK
            else:
                raise

        sagemaker_image_config_dict = self.client.create_app_image_config(
            AppImageConfigName=app_image_config_name,
            KernelGatewayImageConfig={
                'KernelSpecs': kernel_specs,
                'FileSystemConfig': file_system_config
            }
        )
        image_config_arn = sagemaker_image_config_dict['AppImageConfigArn']
        self.logger.info(f"Created SageMaker image config with ARN: {image_config_arn}")

        self.client.update_domain(
            DomainId=self.domain_id,
            DefaultUserSettings={
                'KernelGatewayAppSettings': {
                    'CustomImages': [
                        {
                            'ImageName': image_name,
                            'ImageVersionNumber': image_version,
                            'AppImageConfigName': app_image_config_name
                        }
                    ]
                }
            }
        )

        return Image(image_arn, image_version_arn)

    def wait_for_image_creation(self, image_name):
        self.logger.info(f"Waiting for SageMaker image creation: {image_name}")
        waiter = self.client.get_waiter('image_created')
        waiter.wait(
            ImageName=image_name
        )
        self.logger.info(f"Image created: {image_name}")

    def wait_for_image_version_creation(self, image_name):
        self.logger.info(f"Waiting for the latest version creation of SageMaker image: {image_name}")
        waiter = self.client.get_waiter('image_version_created')
        try:
            waiter.wait(
                ImageName=image_name
            )
        except WaiterError as e:
            self.logger.error("SageMaker image version creation failed", exc_info=1)
            raise ValueError("SageMaker image version creation failed") from e
        self.logger.info(f"Image version created for image: {image_name}")

    def wait_for_image_deletion(self, image_name):
        self.logger.info(f"Waiting for SageMaker image deletion: {image_name}")
        waiter = self.client.get_waiter('image_deleted')
        waiter.wait(
            ImageName=image_name
        )
        self.logger.info(f"Image deleted: {image_name}")


class NotebookInstance:
    logger = logging.getLogger('sagemaker-ssh-helper:NotebookInstance')

    def __init__(self, notebook_name, region_name: str = None):
        self.notebook_name = notebook_name
        self.current_region = region_name or boto3.session.Session().region_name
        self.client = boto3.client('sagemaker', region_name=self.current_region)
        self.ssh_log = SSHLog(region_name=self.current_region)

    def get_instance_ids(self):
        result = SSMManager().get_notebook_instance_ids(self.notebook_name)
        return result

    def get_cloudwatch_url(self):
        raise ValueError("Not implemented")

    def get_metadata_url(self):
        raise ValueError("Not implemented")
