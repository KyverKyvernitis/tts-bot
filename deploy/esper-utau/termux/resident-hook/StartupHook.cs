// Keep the verified ESPER-Utau entry assembly resident without changing its
// rendering algorithm or redistributing another self-contained .NET runtime.
using System.Reflection;
using System.Text;

// .NET startup hooks require this exact global type and method name.
public static class StartupHook
{
    private const string SourceCommit = "2ff1a4692fa88fb1b60933408125632d0c5d8356";
    private const int MaximumRequestCharacters = 65536;
    private const int MaximumArgumentBytes = 16384;
    private static readonly object RequestLock = new();
    private static readonly object ProtocolLock = new();
    private static string? pending;

    private static string? ReadBoundedLine(TextReader input)
    {
        var buffer = new StringBuilder();
        while (true)
        {
            var value = input.Read();
            if (value < 0)
                return buffer.Length == 0 ? null : throw new InvalidDataException("truncated_request");
            if (value == '\n')
                return buffer.ToString();
            if (value > 127 || buffer.Length >= MaximumRequestCharacters)
                throw new InvalidDataException("invalid_request");
            buffer.Append((char)value);
        }
    }

    private static bool ValidId(string value)
    {
        if (value.Length == 0 || value.Length > 64)
            return false;
        foreach (var character in value)
            if (!(character is >= 'a' and <= 'z' or >= 'A' and <= 'Z'
                    or >= '0' and <= '9' or '_' or '-'))
                return false;
        return true;
    }

    private static string[] DecodeArguments(string line, out string? id)
    {
        id = null;
        var fields = line.Split('\t');
        if (fields.Length != 14 || !ValidId(fields[0]))
            throw new ArgumentException("invalid_request");
        id = fields[0];
        var values = new string[13];
        var utf8 = new UTF8Encoding(false, true);
        for (var index = 0; index < values.Length; index++)
        {
            if (fields[index + 1].Length > ((MaximumArgumentBytes + 2) / 3) * 4)
                throw new ArgumentException("argument_too_large");
            var bytes = Convert.FromBase64String(fields[index + 1]);
            if (bytes.Length > MaximumArgumentBytes)
                throw new ArgumentException("argument_too_large");
            var value = utf8.GetString(bytes);
            if (value.Contains('\0'))
                throw new ArgumentException("invalid_argument");
            values[index] = value;
        }
        return values;
    }

    private static void Send(TextWriter protocol, string line)
    {
        lock (ProtocolLock)
        {
            protocol.WriteLine(line);
            protocol.Flush();
        }
    }

    private static string Response(string? id, bool ok, string? error, long elapsed)
    {
        // Ids and error names have a restricted alphabet. No source paths,
        // exception messages or synthesis arguments enter the protocol.
        var identifier = id is null ? "null" : "\"" + id + "\"";
        var failure = error is null ? "" : ",\"error\":\"" + error + "\"";
        return "{\"protocol\":1,\"id\":" + identifier + ",\"ok\":"
            + (ok ? "true" : "false") + ",\"process_id\":" + Environment.ProcessId
            + ",\"pid\":" + Environment.ProcessId + ",\"native_render_ms\":"
            + Math.Max(0, elapsed) + failure + "}";
    }

    public static void Initialize()
    {
        if (Environment.GetEnvironmentVariable("PHONE_WORKER_ESPER_HOOK_PROTOCOL") != "1")
            return;
        var assembly = Assembly.GetEntryAssembly();
        var entry = assembly?.EntryPoint;
        var commandLine = typeof(Environment).GetField("s_commandLineArgs",
            BindingFlags.Static | BindingFlags.NonPublic);
        if (assembly?.GetName().Name != "ESPER-Utau" || entry is null || commandLine is null
                || commandLine.FieldType != typeof(string[]) || entry.GetParameters().Length != 1)
            throw new InvalidOperationException("unsupported_esper_entrypoint");
        var original = Environment.GetCommandLineArgs();
        if (original.Length == 0)
            throw new InvalidOperationException("missing_engine_path");
        var protocol = Console.Out;
        Send(protocol, "{\"protocol\":1,\"ready\":true,\"engine\":\"ESPER-Utau\","
            + "\"source_commit\":\"" + SourceCommit + "\",\"native_resident\":true,"
            + "\"native_streaming\":false,\"request_encoding\":\"tsv-base64-v1\","
            + "\"process_id\":" + Environment.ProcessId + ",\"pid\":" + Environment.ProcessId + "}");

        // Closing the owner pipe ends a render even while the entry assembly
        // is calculating its complete native inverse transform. One pending
        // request bounds memory; requests within a process remain serialized.
        var input = Console.In;
        var reader = new Thread(() =>
        {
            try
            {
                while (true)
                {
                    var line = ReadBoundedLine(input);
                    if (line is null)
                        Environment.Exit(0);
                    lock (RequestLock)
                    {
                        while (pending is not null)
                            Monitor.Wait(RequestLock);
                        pending = line;
                        Monitor.PulseAll(RequestLock);
                    }
                }
            }
            catch
            {
                Environment.Exit(2);
            }
        }) { IsBackground = true, Name = "esper-hook-owner" };
        reader.Start();
        while (true)
        {
            string line;
            lock (RequestLock)
            {
                while (pending is null)
                    Monitor.Wait(RequestLock);
                line = pending;
                pending = null;
                Monitor.PulseAll(RequestLock);
            }
            string? id = null;
            var started = Environment.TickCount64;
            string? error = null;
            try
            {
                var values = DecodeArguments(line, out id);
                var arguments = new string[14];
                arguments[0] = original[0];
                Array.Copy(values, 0, arguments, 1, values.Length);
                commandLine.SetValue(null, arguments);
                // Native warnings cannot corrupt the protocol or fill a
                // diagnostics pipe while its owner is awaiting a response.
                Console.SetOut(TextWriter.Null);
                Console.SetError(TextWriter.Null);
                entry.Invoke(null, new object?[] { Array.Empty<string>() });
            }
            catch (Exception failure)
            {
                var actual = failure is TargetInvocationException wrapped && wrapped.InnerException is not null
                    ? wrapped.InnerException : failure;
                error = actual.GetType().Name;
            }
            finally
            {
                commandLine.SetValue(null, original);
                Console.SetOut(protocol);
            }
            Send(protocol, Response(id, error is null, error, Environment.TickCount64 - started));
        }
    }
}
