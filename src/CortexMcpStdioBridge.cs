using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;

namespace Cortex.Claude
{
    internal sealed class BridgeOptions
    {
        public Uri GatewayUri;
        public TimeSpan RequestTimeout;

        public static BridgeOptions Parse(string[] arguments)
        {
            string gatewayUrl = "http://127.0.0.1:8877/mcp/cortex/";
            int timeoutSeconds = 900;

            for (int i = 0; i < arguments.Length; i++)
            {
                string argument = arguments[i];
                if (String.Equals(argument, "--url", StringComparison.OrdinalIgnoreCase))
                {
                    gatewayUrl = RequireValue(arguments, ref i, argument);
                }
                else if (String.Equals(argument, "--timeout-seconds", StringComparison.OrdinalIgnoreCase))
                {
                    string rawTimeout = RequireValue(arguments, ref i, argument);
                    if (!Int32.TryParse(rawTimeout, out timeoutSeconds) || timeoutSeconds < 5 || timeoutSeconds > 1800)
                    {
                        throw new ArgumentException("--timeout-seconds must be between 5 and 1800.");
                    }
                }
                else
                {
                    throw new ArgumentException("Unexpected bridge argument: " + argument);
                }
            }

            Uri gatewayUri;
            if (!Uri.TryCreate(gatewayUrl, UriKind.Absolute, out gatewayUri) ||
                !gatewayUri.IsLoopback ||
                !String.Equals(gatewayUri.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase))
            {
                throw new ArgumentException("The Cortex gateway URL must be an http:// loopback address.");
            }

            if (!gatewayUri.AbsolutePath.EndsWith("/", StringComparison.Ordinal))
            {
                UriBuilder builder = new UriBuilder(gatewayUri);
                builder.Path = builder.Path + "/";
                gatewayUri = builder.Uri;
            }

            BridgeOptions options = new BridgeOptions();
            options.GatewayUri = gatewayUri;
            options.RequestTimeout = TimeSpan.FromSeconds(timeoutSeconds);
            return options;
        }

        private static string RequireValue(string[] arguments, ref int index, string name)
        {
            if (index + 1 >= arguments.Length || String.IsNullOrWhiteSpace(arguments[index + 1]))
            {
                throw new ArgumentException("Missing value for " + name + ".");
            }

            index++;
            return arguments[index];
        }
    }

    internal sealed class RequestMetadata
    {
        public bool HasId;
        public object Id;
        public string SerializedId;
        public string Method;
        public string RequestedProtocolVersion;
    }

    internal sealed class GatewayHttpException : Exception
    {
        public readonly int StatusCode;

        public GatewayHttpException(HttpStatusCode statusCode, string reason)
            : base("Cortex gateway returned HTTP " + ((int)statusCode).ToString() +
                   (String.IsNullOrWhiteSpace(reason) ? "." : " (" + Sanitize(reason) + ")."))
        {
            StatusCode = (int)statusCode;
        }

        private static string Sanitize(string value)
        {
            StringBuilder result = new StringBuilder();
            foreach (char character in value)
            {
                if (result.Length >= 120)
                {
                    break;
                }

                if (character >= 32 && character != 127)
                {
                    result.Append(character);
                }
            }

            return result.ToString();
        }
    }

    internal sealed class CortexMcpBridge : IDisposable
    {
        private readonly BridgeOptions _options;
        private readonly string _bearerToken;
        private readonly HttpClient _client;
        private readonly JavaScriptSerializer _json;
        private readonly TextWriter _output;
        private string _sessionId;
        private string _protocolVersion;
        private bool _disposed;

        public CortexMcpBridge(BridgeOptions options, string bearerToken, TextWriter output)
        {
            _options = options;
            _bearerToken = bearerToken;
            _output = output;
            _json = new JavaScriptSerializer();
            _json.MaxJsonLength = Int32.MaxValue;
            _json.RecursionLimit = 256;

            HttpClientHandler handler = new HttpClientHandler();
            handler.UseProxy = false;
            handler.AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate;
            _client = new HttpClient(handler);
            _client.Timeout = Timeout.InfiniteTimeSpan;
        }

        public void Run(TextReader input)
        {
            string line;
            while ((line = input.ReadLine()) != null)
            {
                if (String.IsNullOrWhiteSpace(line))
                {
                    continue;
                }

                ProcessLine(line);
            }
        }

        private void ProcessLine(string line)
        {
            RequestMetadata metadata;
            try
            {
                metadata = ParseRequest(line);
            }
            catch (Exception)
            {
                WriteError(null, -32700, "Invalid JSON received by the Cortex bridge.");
                return;
            }

            if (String.IsNullOrWhiteSpace(_bearerToken))
            {
                if (metadata.HasId)
                {
                    WriteError(metadata.Id, -32002,
                        "HINDSIGHT_MCP_API_KEY is not set in the Windows user environment.");
                }
                return;
            }

            try
            {
                SendToGateway(line, metadata);
                if (String.Equals(metadata.Method, "initialize", StringComparison.Ordinal) &&
                    !String.IsNullOrWhiteSpace(metadata.RequestedProtocolVersion))
                {
                    _protocolVersion = metadata.RequestedProtocolVersion;
                }
            }
            catch (GatewayHttpException exception)
            {
                if (metadata.HasId)
                {
                    WriteError(metadata.Id, -32000, exception.Message);
                }
            }
            catch (TimeoutException)
            {
                if (metadata.HasId)
                {
                    WriteError(metadata.Id, -32001, "Timed out waiting for the local Cortex gateway.");
                }
            }
            catch (Exception)
            {
                if (metadata.HasId)
                {
                    WriteError(metadata.Id, -32001, "Could not reach the local Cortex gateway.");
                }
            }
        }

        private RequestMetadata ParseRequest(string line)
        {
            object value = _json.DeserializeObject(line);
            Dictionary<string, object> request = value as Dictionary<string, object>;
            if (request == null)
            {
                throw new InvalidDataException("A JSON-RPC request must be an object.");
            }

            RequestMetadata metadata = new RequestMetadata();
            object id;
            if (request.TryGetValue("id", out id))
            {
                metadata.HasId = true;
                metadata.Id = id;
                metadata.SerializedId = _json.Serialize(id);
            }

            object method;
            if (request.TryGetValue("method", out method))
            {
                metadata.Method = method as string;
            }

            if (String.Equals(metadata.Method, "initialize", StringComparison.Ordinal))
            {
                object rawParameters;
                Dictionary<string, object> parameters;
                object rawProtocolVersion;
                if (request.TryGetValue("params", out rawParameters) &&
                    (parameters = rawParameters as Dictionary<string, object>) != null &&
                    parameters.TryGetValue("protocolVersion", out rawProtocolVersion))
                {
                    metadata.RequestedProtocolVersion = rawProtocolVersion as string;
                }
            }

            return metadata;
        }

        private void SendToGateway(string payload, RequestMetadata metadata)
        {
            using (CancellationTokenSource cancellation = new CancellationTokenSource(_options.RequestTimeout))
            using (HttpRequestMessage request = BuildRequest(HttpMethod.Post))
            {
                request.Content = new StringContent(payload, new UTF8Encoding(false), "application/json");

                HttpResponseMessage response;
                try
                {
                    response = _client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead,
                        cancellation.Token).GetAwaiter().GetResult();
                }
                catch (OperationCanceledException)
                {
                    throw new TimeoutException();
                }

                using (response)
                {
                    CaptureSession(response);
                    if (!response.IsSuccessStatusCode)
                    {
                        throw new GatewayHttpException(response.StatusCode, response.ReasonPhrase);
                    }

                    if (response.StatusCode == HttpStatusCode.Accepted ||
                        response.StatusCode == HttpStatusCode.NoContent ||
                        response.Content == null)
                    {
                        if (metadata.HasId)
                        {
                            WriteError(metadata.Id, -32000,
                                "The Cortex gateway returned no JSON-RPC response.");
                        }
                        return;
                    }

                    string mediaType = response.Content.Headers.ContentType == null
                        ? String.Empty
                        : response.Content.Headers.ContentType.MediaType;

                    if (String.Equals(mediaType, "text/event-stream", StringComparison.OrdinalIgnoreCase))
                    {
                        ReadEventStream(response, metadata, cancellation.Token);
                    }
                    else
                    {
                        string body;
                        try
                        {
                            body = response.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                        }
                        catch (OperationCanceledException)
                        {
                            throw new TimeoutException();
                        }

                        if (!String.IsNullOrWhiteSpace(body))
                        {
                            WriteJsonMessage(body);
                        }
                        else if (metadata.HasId)
                        {
                            WriteError(metadata.Id, -32000,
                                "The Cortex gateway returned an empty JSON-RPC response.");
                        }
                    }
                }
            }
        }

        private HttpRequestMessage BuildRequest(HttpMethod method)
        {
            HttpRequestMessage request = new HttpRequestMessage(method, _options.GatewayUri);
            request.Headers.TryAddWithoutValidation("Authorization", "Bearer " + _bearerToken);
            request.Headers.TryAddWithoutValidation("Accept", "application/json, text/event-stream");

            if (!String.IsNullOrWhiteSpace(_sessionId))
            {
                request.Headers.TryAddWithoutValidation("Mcp-Session-Id", _sessionId);
            }

            if (!String.IsNullOrWhiteSpace(_protocolVersion))
            {
                request.Headers.TryAddWithoutValidation("MCP-Protocol-Version", _protocolVersion);
            }

            return request;
        }

        private void CaptureSession(HttpResponseMessage response)
        {
            IEnumerable<string> values;
            if (response.Headers.TryGetValues("Mcp-Session-Id", out values))
            {
                foreach (string value in values)
                {
                    if (!String.IsNullOrWhiteSpace(value))
                    {
                        _sessionId = value.Trim();
                        break;
                    }
                }
            }
        }

        private void ReadEventStream(HttpResponseMessage response, RequestMetadata metadata,
            CancellationToken cancellationToken)
        {
            using (Stream stream = response.Content.ReadAsStreamAsync().GetAwaiter().GetResult())
            using (StreamReader reader = new StreamReader(stream, new UTF8Encoding(false), true, 4096, false))
            {
                StringBuilder data = new StringBuilder();
                while (true)
                {
                    string line = ReadLineWithCancellation(reader, cancellationToken);
                    if (line == null)
                    {
                        if (data.Length > 0)
                        {
                            EmitEvent(data.ToString(), metadata);
                        }
                        break;
                    }

                    if (line.Length == 0)
                    {
                        if (data.Length > 0)
                        {
                            bool matched = EmitEvent(data.ToString(), metadata);
                            data.Length = 0;
                            if (matched)
                            {
                                break;
                            }
                        }
                        continue;
                    }

                    if (line.StartsWith("data:", StringComparison.Ordinal))
                    {
                        string value = line.Substring(5);
                        if (value.StartsWith(" ", StringComparison.Ordinal))
                        {
                            value = value.Substring(1);
                        }

                        if (data.Length > 0)
                        {
                            data.Append('\n');
                        }
                        data.Append(value);
                    }
                }
            }
        }

        private string ReadLineWithCancellation(StreamReader reader, CancellationToken cancellationToken)
        {
            Task<string> readTask = reader.ReadLineAsync();
            Task cancellationTask = Task.Delay(Timeout.Infinite, cancellationToken);
            Task completed = Task.WhenAny(readTask, cancellationTask).GetAwaiter().GetResult();
            if (completed != readTask)
            {
                throw new TimeoutException();
            }

            return readTask.GetAwaiter().GetResult();
        }

        private bool EmitEvent(string rawData, RequestMetadata request)
        {
            if (String.Equals(rawData.Trim(), "[DONE]", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            object parsed = _json.DeserializeObject(rawData);
            string normalized = _json.Serialize(parsed);
            _output.WriteLine(normalized);
            _output.Flush();

            if (!request.HasId)
            {
                return true;
            }

            Dictionary<string, object> message = parsed as Dictionary<string, object>;
            object responseId;
            return message != null &&
                   message.TryGetValue("id", out responseId) &&
                   String.Equals(_json.Serialize(responseId), request.SerializedId, StringComparison.Ordinal);
        }

        private void WriteJsonMessage(string rawJson)
        {
            object parsed = _json.DeserializeObject(rawJson);
            _output.WriteLine(_json.Serialize(parsed));
            _output.Flush();
        }

        private void WriteError(object id, int code, string message)
        {
            Dictionary<string, object> errorBody = new Dictionary<string, object>();
            errorBody["code"] = code;
            errorBody["message"] = message;

            Dictionary<string, object> response = new Dictionary<string, object>();
            response["jsonrpc"] = "2.0";
            response["id"] = id;
            response["error"] = errorBody;
            _output.WriteLine(_json.Serialize(response));
            _output.Flush();
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }
            _disposed = true;

            if (!String.IsNullOrWhiteSpace(_sessionId) && !String.IsNullOrWhiteSpace(_bearerToken))
            {
                try
                {
                    using (CancellationTokenSource cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
                    using (HttpRequestMessage request = BuildRequest(HttpMethod.Delete))
                    using (HttpResponseMessage response = _client.SendAsync(request,
                        HttpCompletionOption.ResponseHeadersRead, cancellation.Token).GetAwaiter().GetResult())
                    {
                    }
                }
                catch (Exception)
                {
                    // Session cleanup is best effort and must never pollute MCP stdout.
                }
            }

            _client.Dispose();
        }
    }

    internal static class Program
    {
        [STAThread]
        private static int Main(string[] arguments)
        {
            TextWriter output = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false));
            TextReader input = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false), true);

            BridgeOptions options;
            try
            {
                options = BridgeOptions.Parse(arguments);
            }
            catch (Exception exception)
            {
                WriteStartupError(output, "Cortex bridge configuration error: " + exception.Message);
                return 2;
            }

            string token = Environment.GetEnvironmentVariable(
                "HINDSIGHT_MCP_API_KEY", EnvironmentVariableTarget.User);

            try
            {
                ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
                using (CortexMcpBridge bridge = new CortexMcpBridge(options, token, output))
                {
                    bridge.Run(input);
                }
                return 0;
            }
            catch (Exception)
            {
                WriteStartupError(output, "The Cortex MCP bridge stopped unexpectedly.");
                return 1;
            }
            finally
            {
                output.Flush();
                output.Dispose();
                input.Dispose();
            }
        }

        private static void WriteStartupError(TextWriter output, string message)
        {
            JavaScriptSerializer json = new JavaScriptSerializer();
            Dictionary<string, object> error = new Dictionary<string, object>();
            error["code"] = -32003;
            error["message"] = message;

            Dictionary<string, object> response = new Dictionary<string, object>();
            response["jsonrpc"] = "2.0";
            response["id"] = null;
            response["error"] = error;
            output.WriteLine(json.Serialize(response));
            output.Flush();
        }
    }
}
