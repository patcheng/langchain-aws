"""Sagemaker InvokeEndpoint API."""

import io
import logging
import re
from typing import (
    Any,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
)

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.llms import LLM
from langchain_core.outputs import GenerationChunk
from pydantic import ConfigDict, model_validator
from typing_extensions import Self

from langchain_aws.utils import ContentHandlerBase

logger = logging.getLogger(__name__)


def enforce_stop_tokens(text: str, stop: List[str]) -> str:
    """Cut off the text as soon as any stop words occur."""
    return re.split("|".join(stop), text, maxsplit=1)[0]


class LineIterator:
    """
    A helper class for parsing the byte stream input.

    The output of the model will be in the following format:

    b'{"outputs": [" a"]}\n'
    b'{"outputs": [" challenging"]}\n'
    b'{"outputs": [" problem"]}\n'
    ...

    While usually each PayloadPart event from the event stream will
    contain a byte array with a full json, this is not guaranteed
    and some of the json objects may be split acrossPayloadPart events.

    For example:

    {'PayloadPart': {'Bytes': b'{"outputs": '}}
    {'PayloadPart': {'Bytes': b'[" problem"]}\n'}}


    This class accounts for this by concatenating bytes written via the 'write' function
    and then exposing a method which will return lines (ending with a '\n' character)
    within the buffer via the 'scan_lines' function.
    It maintains the position of the last read position to ensure
    that previous bytes are not exposed again.

    For more details see:
    https://aws.amazon.com/blogs/machine-learning/elevating-the-generative-ai-experience-introducing-streaming-support-in-amazon-sagemaker-hosting/
    """

    def __init__(self, stream: Any) -> None:
        self.byte_iterator = iter(stream)
        self.buffer = io.BytesIO()
        self.read_pos = 0

    def __iter__(self) -> "LineIterator":
        return self

    def __next__(self) -> Any:
        while True:
            self.buffer.seek(self.read_pos)
            line = self.buffer.readline()
            if line and line[-1] == ord("\n"):
                self.read_pos += len(line)
                return line[:-1]
            try:
                chunk = next(self.byte_iterator)
            except StopIteration:
                if self.read_pos < self.buffer.getbuffer().nbytes:
                    continue
                raise
            if "PayloadPart" not in chunk:
                # Unknown Event Type
                continue
            self.buffer.seek(0, io.SEEK_END)
            self.buffer.write(chunk["PayloadPart"]["Bytes"])


class LLMContentHandler(ContentHandlerBase[str, str]):
    """Content handler for LLM class."""


class SagemakerEndpoint(LLM):
    """Sagemaker Inference Endpoint models.

    To use, you must supply the endpoint name from your deployed
    Sagemaker model & the region where it is deployed.

    To authenticate, the AWS client uses the following methods to
    automatically load credentials:
    https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html

    If a specific credential profile should be used, you must pass
    the name of the profile from the ~/.aws/credentials file that is to be used.

    Make sure the credentials / roles used have the required policies to
    access the Sagemaker endpoint.
    See: https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies.html
    """

    """
    Args:        

        region_name: The aws region e.g., `us-west-2`.
            Fallsback to AWS_DEFAULT_REGION env variable
            or region specified in ~/.aws/config.

        credentials_profile_name: The name of the profile in the ~/.aws/credentials
            or ~/.aws/config files, which has either access keys or role information
            specified. If not specified, the default credential profile or, if on an
            EC2 instance, credentials from IMDS will be used.

        client: boto3 client for Sagemaker Endpoint

        content_handler: Implementation for model specific LLMContentHandler 


    Example:
        .. code-block:: python

            from langchain_community.llms import SagemakerEndpoint
            endpoint_name = (
                "my-endpoint-name"
            )
            region_name = (
                "us-west-2"
            )
            credentials_profile_name = (
                "default"
            )
            se = SagemakerEndpoint(
                endpoint_name=endpoint_name,
                region_name=region_name,
                credentials_profile_name=credentials_profile_name
            )
        
            # Usage with Inference Component
            se = SagemakerEndpoint(
                endpoint_name=endpoint_name,
                inference_component_name=inference_component_name,
                region_name=region_name,
                credentials_profile_name=credentials_profile_name
            )

        #Use with boto3 client
            client = boto3.client(
                        "sagemaker-runtime",
                        region_name=region_name
                    )

            se = SagemakerEndpoint(
                endpoint_name=endpoint_name,
                client=client
            )

    """
    client: Any = None
    """Boto3 client for sagemaker runtime"""

    endpoint_name: str = ""
    """The name of the endpoint from the deployed Sagemaker model.
    Must be unique within an AWS Region."""

    inference_component_name: Optional[str] = None
    """Optional name of the inference component to invoke 
    if specified with endpoint name."""

    region_name: str = ""
    """The aws region where the Sagemaker model is deployed, eg. `us-west-2`."""

    credentials_profile_name: Optional[str] = None
    """The name of the profile in the ~/.aws/credentials or ~/.aws/config files, which
    has either access keys or role information specified.
    If not specified, the default credential profile or, if on an EC2 instance,
    credentials from IMDS will be used.
    See: https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html
    """

    content_handler: LLMContentHandler
    """The content handler class that provides an input and
    output transform functions to handle formats between LLM
    and the endpoint.
    """

    streaming: bool = False
    """Whether to stream the results."""

    """
     Example:
        .. code-block:: python

        from langchain_community.llms.sagemaker_endpoint import LLMContentHandler

        class ContentHandler(LLMContentHandler):
                content_type = "application/json"
                accepts = "application/json"

                def transform_input(self, prompt: str, model_kwargs: Dict) -> bytes:
                    input_str = json.dumps({prompt: prompt, **model_kwargs})
                    return input_str.encode('utf-8')
                
                def transform_output(self, output: bytes) -> str:
                    response_json = json.loads(output.read().decode("utf-8"))
                    return response_json[0]["generated_text"]
    """

    model_kwargs: Optional[Dict] = None
    """Keyword arguments to pass to the model."""

    endpoint_kwargs: Optional[Dict] = None
    """Optional attributes passed to the invoke_endpoint
    function. See `boto3`_. docs for more info.
    .. _boto3: <https://boto3.amazonaws.com/v1/documentation/api/latest/index.html>
    """

    model_config = ConfigDict(
        extra="forbid",
    )

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        """Dont do anything if client provided externally"""
        if self.client is not None:
            return self

        """Validate that AWS credentials to and python package exists in environment."""
        try:
            import boto3

            try:
                if self.credentials_profile_name is not None:
                    session = boto3.Session(profile_name=self.credentials_profile_name)
                else:
                    # use default credentials
                    session = boto3.Session()

                self.client = session.client(
                    "sagemaker-runtime", region_name=self.region_name
                )

            except Exception as e:
                raise ValueError(
                    "Could not load credentials to authenticate with AWS client. "
                    "Please check that credentials in the specified "
                    "profile name are valid."
                ) from e

        except ImportError:
            raise ImportError(
                "Could not import boto3 python package. "
                "Please install it with `pip install boto3`."
            )
        return self

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        _model_kwargs = self.model_kwargs or {}
        return {
            **{"endpoint_name": self.endpoint_name},
            **{"inference_component_name": self.inference_component_name},
            **{"model_kwargs": _model_kwargs},
        }

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "sagemaker_endpoint"

    def _stream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[GenerationChunk]:
        _model_kwargs = self.model_kwargs or {}
        _model_kwargs = {**_model_kwargs, **kwargs}
        _endpoint_kwargs = self.endpoint_kwargs or {}

        invocation_params = {
            "EndpointName": self.endpoint_name,
            "Body": self.content_handler.transform_input(prompt, _model_kwargs),
            "ContentType": self.content_handler.content_type,
            **_endpoint_kwargs,
        }

        # If inference_component_name is specified, append it to invocation_params
        if self.inference_component_name:
            invocation_params["InferenceComponentName"] = self.inference_component_name

        try:
            resp = self.client.invoke_endpoint_with_response_stream(**invocation_params)
            iterator = LineIterator(resp["Body"])

            for line in iterator:
                text = self.content_handler.transform_output(line)

                if stop is not None:
                    text = enforce_stop_tokens(text, stop)

                if text:
                    chunk = GenerationChunk(text=text)
                    yield chunk
                    if run_manager:
                        run_manager.on_llm_new_token(chunk.text)

        except Exception as e:
            logger.exception("Error raised by streaming inference endpoint")
            if run_manager is not None:
                run_manager.on_llm_error(e)
            raise e

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """Call out to SageMaker inference endpoint or inference component
            of SageMaker inference endpoint.

        Args:
            prompt: The prompt to pass into the model.
            stop: Optional list of stop words to use when generating.

        Returns:
            The string generated by the model.

        Example:
            .. code-block:: python

                response = se("Tell me a joke.")
        """
        _model_kwargs = self.model_kwargs or {}
        _model_kwargs = {**_model_kwargs, **kwargs}
        _endpoint_kwargs = self.endpoint_kwargs or {}

        body = self.content_handler.transform_input(prompt, _model_kwargs)
        content_type = self.content_handler.content_type
        accepts = self.content_handler.accepts

        invocation_params = {
            "EndpointName": self.endpoint_name,
            "Body": body,
            "ContentType": content_type,
            "Accept": accepts,
            **_endpoint_kwargs,
        }

        # If inference_compoent_name is specified, append it to invocation_params
        if self.inference_component_name:
            invocation_params["InferenceComponentName"] = self.inference_component_name

        if self.streaming and run_manager:
            completion: str = ""
            for chunk in self._stream(prompt, stop, run_manager, **kwargs):
                completion += chunk.text
            return completion

        try:
            response = self.client.invoke_endpoint(**invocation_params)
        except Exception as e:
            logger.exception("Error raised by inference endpoint")
            if run_manager is not None:
                run_manager.on_llm_error(e)
            raise e

        text = self.content_handler.transform_output(response["Body"])
        if stop is not None:
            text = enforce_stop_tokens(text, stop)

        return text
