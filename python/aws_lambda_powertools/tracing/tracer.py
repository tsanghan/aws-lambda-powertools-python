import copy
import functools
import inspect
import logging
import os
from distutils.util import strtobool
from typing import Any, Callable, Dict, Generic, List

import aws_xray_sdk
import aws_xray_sdk.core

is_cold_start = True
logger = logging.getLogger(__name__)

# FIXME - Add get current subsegment()
# FIXME - Add subsegment type

class Tracer:
    """Tracer using AWS-XRay to provide decorators with known defaults for Lambda functions

    When running locally, it detects whether it's running via SAM CLI,
    and if it is it returns dummy segments/subsegments instead.

    By default, it patches all available libraries supported by X-Ray SDK. Patching is
    automatically disabled when running locally via SAM CLI or by any other means. \n
    Ref: https://docs.aws.amazon.com/xray-sdk-for-python/latest/reference/thirdparty.html

    Tracer keeps a copy of its configuration as it can be instantiated more than once. This
    is useful when you are using your own middlewares and want to utilize an existing Tracer.
    Make sure to set `auto_patch=False` in subsequent Tracer instances to avoid double patching.

    Environment variables
    ---------------------
    POWERTOOLS_TRACE_DISABLED : str
        disable tracer (e.g. `"true", "True", "TRUE"`)
    POWERTOOLS_SERVICE_NAME : str
        service name

    Parameters
    ----------
    service: str
        Service name that will be appended in all tracing metadata
    auto_patch: bool
        Patch existing imported modules during initialization, by default True
    disabled: bool
        Flag to explicitly disable tracing, useful when running/testing locally
        `Env POWERTOOLS_TRACE_DISABLED="true"`
    patch_modules: List
        List of modules supported by tracing provider to patch, by default all modules are patched

    Example
    -------
    **A Lambda function using Tracer**

        from aws_lambda_powertools.tracing import Tracer
        tracer = Tracer(service="greeting")

        @tracer.capture_method
        def greeting(name: str) -> Dict:
            return {
                "name": name
            }

        @tracer.capture_lambda_handler
        def handler(event: dict, context: Any) -> Dict:
            print("Received event from Lambda...")
            response = greeting(name="Heitor")
            return response

    **Booking Lambda function using Tracer that adds additional annotation/metadata**

        from aws_lambda_powertools.tracing import Tracer
        tracer = Tracer(service="booking")

        @tracer.capture_method
        def confirm_booking(booking_id: str) -> Dict:
                resp = add_confirmation(booking_id)

                tracer.put_annotation("BookingConfirmation", resp['requestId'])
                tracer.put_metadata("Booking confirmation", resp)

                return resp

        @tracer.capture_lambda_handler
        def handler(event: dict, context: Any) -> Dict:
            print("Received event from Lambda...")
            response = greeting(name="Heitor")
            return response

    **A Lambda function using service name via POWERTOOLS_SERVICE_NAME**

        export POWERTOOLS_SERVICE_NAME="booking"
        from aws_lambda_powertools.tracing import Tracer
        tracer = Tracer()

        @tracer.capture_lambda_handler
        def handler(event: dict, context: Any) -> Dict:
            print("Received event from Lambda...")
            response = greeting(name="Lessa")
            return response

    **Reuse an existing instance of Tracer anywhere in the code**

        # lambda_handler.py
        from aws_lambda_powertools.tracing import Tracer
        tracer = Tracer()

        @tracer.capture_lambda_handler
        def handler(event: dict, context: Any) -> Dict:
            ...

        # utils.py
        from aws_lambda_powertools.tracing import Tracer
        tracer = Tracer()
        ...

    Returns
    -------
    Tracer
        Tracer instance with imported modules patched

    Limitations
    -----------
    * Async handler and methods not supported
    """

    _default_config = {
        "service": "service_undefined",
        "disabled": False,
        "provider": None,
        "auto_patch": True,
        "patch_modules": None,
        "provider": aws_xray_sdk.core.xray_recorder,
    }
    _config = copy.copy(_default_config)

    def __init__(
        self,
        service: str = None,
        disabled: bool = None,
        auto_patch: bool = None,
        patch_modules: List = None,
        provider: aws_xray_sdk.core.xray_recorder = None,
    ):
        self.__build_config(
            service=service, disabled=disabled, auto_patch=auto_patch, patch_modules=patch_modules, provider=provider
        )
        self.provider = self._config["provider"]
        self.disabled = self._config["disabled"]
        self.service = self._config["service"]
        self.auto_patch = self._config["auto_patch"]

        if self.disabled:
            self.disable_tracing_provider()

        if self.auto_patch:
            self.patch(modules=patch_modules)

    def create_subsegment(self, name: str) -> aws_xray_sdk.core.models.subsegment:
        """Creates subsegment/span with a given name

        Parameters
        ----------
        name : str
            Subsegment name

        Example
        -------

        **Creates a subsegment**

            self.create_subsegment(name="a meaningful name")

        Returns
        -------
        aws_xray_sdk.core.models.subsegment
            AWS X-Ray Subsegment
        """
        # Will no longer be needed once #155 is resolved
        # https://github.com/aws/aws-xray-sdk-python/issues/155
        subsegment = self.provider.begin_subsegment(name=name)
        global is_cold_start
        if is_cold_start:
            logger.debug("Annotating cold start")
            subsegment.put_annotation(key="ColdStart", value=True)
            is_cold_start = False

        return subsegment

    def end_subsegment(self):
        """Ends an existing subsegment"""
        # FIXME - Receive subsegment to close
        if self.disabled:
            logger.debug("Tracing has been disabled, aborting end_subsegment")
            return

        self.provider.end_subsegment()

    def put_annotation(self, key: str, value: Any):
        """Adds annotation to existing segment or subsegment

        Example
        -------
        Custom annotation for a pseudo service named payment

            tracer = Tracer(service="payment")
            tracer.put_annotation("PaymentStatus", "CONFIRMED")

        Parameters
        ----------
        key : str
            Annotation key (e.g. PaymentStatus)
        value : any
            Value for annotation (e.g. "CONFIRMED")
        """
        # Will no longer be needed once #155 is resolved
        # https://github.com/aws/aws-xray-sdk-python/issues/155
        if self.disabled:
            logger.debug("Tracing has been disabled, aborting put_annotation")
            return

        logger.debug(f"Annotating on key '{key}'' with '{value}''")
        self.provider.put_annotation(key=key, value=value)

    def put_metadata(self, key: str, value: Any, namespace: str = None):
        """Adds metadata to existing segment or subsegment

        Parameters
        ----------
        key : str
            Metadata key
        value : any
            Value for metadata
        namespace : str, optional
            Namespace that metadata will lie under, by default None

        Example
        -------
        Custom metadata for a pseudo service named payment

            tracer = Tracer(service="payment")
            response = collect_payment()
            tracer.put_metadata("Payment collection", response)
        """
        if self.disabled:
            logger.debug("Tracing has been disabled, aborting put_metadata")
            return

        namespace = namespace or self.service
        logger.debug(f"Adding metadata on key '{key}'' with '{value}'' at namespace '{namespace}''")
        self.provider.put_metadata(key=key, value=value, namespace=namespace)

    def patch(self, modules: List[str] = None):
        """Patch modules for instrumentation.

        Patches all supported modules by default if none are given.

        Parameters
        ----------
        modules : List[str]
            List of modules to be patched, optional by default
        """
        if self.disabled:
            logger.debug("Tracing has been disabled, aborting patch")
            return

        if modules is None:
            aws_xray_sdk.core.patch_all()
        else:
            aws_xray_sdk.core.patch(modules)

    def disable_tracing_provider(self):
        """Forcefully disables tracing"""
        logger.debug("Disabling tracer provider...")
        aws_xray_sdk.global_sdk_config.set_sdk_enabled(False)

    def capture_lambda_handler(self, lambda_handler: Callable[[Dict, Any], Any] = None):
        """Decorator to create subsegment for lambda handlers

        As Lambda follows (event, context) signature we can remove some of the boilerplate
        and also capture any exception any Lambda function throws or its response as metadata

        Example
        -------
        **Lambda function using capture_lambda_handler decorator**

            tracer = Tracer(service="payment")
            @tracer.capture_lambda_handler
            def handler(event, context)

        Parameters
        ----------
        method : Callable
            Method to annotate on

        Raises
        ------
        err
            Exception raised by method
        """

        @functools.wraps(lambda_handler)
        def decorate(event, context):
            # FIXME - Use newly created subsegment to avoid race conditions
            self.create_subsegment(name=f"## {lambda_handler.__name__}")
            try:
                logger.debug("Calling lambda handler")
                response = lambda_handler(event, context)
                logger.debug("Received lambda handler response successfully")
                logger.debug(response)
                if response:
                    self.put_metadata("lambda handler response", response)
            except Exception as err:
                logger.exception("Exception received from lambda handler", exc_info=True)
                self.put_metadata(f"{self.service}_error", err)
                raise
            finally:
                self.end_subsegment()

            return response

        return decorate

    def capture_method(self, method: Callable = None):
        """Decorator to create subsegment for arbitrary functions

        It also captures both response and exceptions as metadata
        and creates a subsegment named `## <method_name>`

        Example
        -------
        **Custom function using capture_method decorator**

            tracer = Tracer(service="payment")
            @tracer.capture_method
            def some_function()

        Parameters
        ----------
        method : Callable
            Method to annotate on

        Raises
        ------
        err
            Exception raised by method
        """

        @functools.wraps(method)
        def decorate(*args, **kwargs):
            method_name = f"{method.__name__}"
            # FIXME - Use newly created subsegment to avoid race conditions
            self.create_subsegment(name=f"## {method_name}")

            try:
                logger.debug(f"Calling method: {method_name}")
                response = method(*args, **kwargs)
                logger.debug(f"Received {method_name} response successfully")
                logger.debug(response)
                if response is not None:
                    self.put_metadata(f"{method_name} response", response)
            except Exception as err:
                logger.exception(f"Exception received from '{method_name}'' method", exc_info=True)
                self.put_metadata(f"{method_name} error", err)
                raise
            finally:
                self.end_subsegment()

            return response

        return decorate

    def __is_trace_disabled(self) -> bool:
        """Detects whether trace has been disabled

        Tracing is automatically disabled in the following conditions:

        1. Explicitly disabled via `TRACE_DISABLED` environment variable
        2. Running in Lambda Emulators, or locally where X-Ray Daemon will not be listening
        3. Explicitly disabled via constructor e.g `Tracer(disabled=True)`

        Returns
        -------
        bool
        """
        logger.debug("Verifying whether Tracing has been disabled")
        is_lambda_sam_cli = os.getenv("AWS_SAM_LOCAL")
        env_option = str(os.getenv("POWERTOOLS_TRACE_DISABLED", "false"))
        disabled_env = strtobool(env_option)

        if disabled_env:
            logger.debug("Tracing has been disabled via env var POWERTOOLS_TRACE_DISABLED")
            return disabled_env

        if is_lambda_sam_cli:
            logger.debug("Running under SAM CLI env or not in Lambda env; disabling Tracing")
            return True

        return False

    def __build_config(
        self,
        service: str = None,
        disabled: bool = None,
        auto_patch: bool = None,
        patch_modules: List = None,
        provider: aws_xray_sdk.core.xray_recorder = None,
    ):
        """ Populates Tracer config for new and existing initializations """
        is_disabled = disabled if disabled is not None else self.__is_trace_disabled()
        is_service = service if service is not None else os.getenv("POWERTOOLS_SERVICE_NAME")

        self._config["provider"] = provider if provider is not None else self._config["provider"]
        self._config["auto_patch"] = auto_patch if auto_patch is not None else self._config["auto_patch"]
        self._config["service"] = is_service if is_service else self._config["service"]
        self._config["disabled"] = is_disabled if is_disabled else self._config["disabled"]
        self._config["patch_modules"] = patch_modules if patch_modules else self._config["patch_modules"]

    @classmethod
    def _reset_config(cls):
        cls._config = copy.copy(cls._default_config)
