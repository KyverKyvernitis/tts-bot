using System.Diagnostics;
using System.Collections.Concurrent;
using System.Text;
using System.Text.Json;

namespace EsperResident;

internal static class Program
{
    private const string SourceCommit = "2ff1a4692fa88fb1b60933408125632d0c5d8356";
    private const int MaximumRequestBytes = 65536;
    private const int MaximumArgumentLength = 16384;

    private static string[] RenderArguments(string[] values)
    {
        if (values.Length != 13 || values.Any(value => value.Length > MaximumArgumentLength || value.Contains('\0')))
            throw new ArgumentException("invalid_arguments");
        // ArgParser locates the unchanged pinned esper-config.ini beside argv[0].
        return new[] { Path.Combine(AppContext.BaseDirectory, "Esper.Resident") }.Concat(values).ToArray();
    }

    private static string? ReadBoundedLine(TextReader input)
    {
        var result = new StringBuilder();
        while (true)
        {
            var character = input.Read();
            if (character < 0)
                return result.Length == 0 ? null : throw new InvalidDataException("truncated_request");
            if (character == '\n')
                break;
            if (result.Length >= MaximumRequestBytes)
                throw new InvalidDataException("request_too_large");
            result.Append((char)character);
        }
        var line = result.ToString();
        if (Encoding.UTF8.GetByteCount(line) > MaximumRequestBytes)
            throw new InvalidDataException("request_too_large");
        return line;
    }

    private static void Send(TextWriter protocol, object message)
    {
        lock (protocol)
        {
            protocol.WriteLine(JsonSerializer.Serialize(message));
            protocol.Flush();
        }
    }

    private static int Main(string[] args)
    {
        // Upstream and its dependencies write diagnostics to Console.Out.
        // Keep the protocol writer separately and route every diagnostic to stderr.
        var protocol = Console.Out;
        Console.SetOut(Console.Error);
        if (args.Length == 14 && args[0] == "--one-shot")
        {
            NativeRender.Render(RenderArguments(args[1..]));
            return 0;
        }
        if (args.Length != 1 || args[0] != "--server")
            return 2;
        Send(protocol, new { protocol = 1, ready = true, engine = "ESPER-Utau",
            source_commit = SourceCommit, native_resident = true, native_streaming = false,
            process_id = Environment.ProcessId });
        // The owner controls this pipe. A dedicated reader observes its EOF
        // during an expensive native inverse transform, so a killed worker
        // cannot leave a detached .NET/PRoot render consuming the phone's CPU.
        using var requests = new BlockingCollection<string>(1);
        var reader = new Thread(() =>
        {
            try
            {
                while (true)
                {
                    var line = ReadBoundedLine(Console.In);
                    if (line is null)
                        Environment.Exit(0);
                    requests.Add(line!);
                }
            }
            catch (Exception error)
            {
                Send(protocol, new { protocol = 1, ok = false, error = error.GetType().Name });
                Environment.Exit(2);
            }
        }) { IsBackground = true, Name = "esper-owner-pipe" };
        reader.Start();
        foreach (var line in requests.GetConsumingEnumerable())
        {
            JsonElement id = default;
            try
            {
                using var document = JsonDocument.Parse(line, new JsonDocumentOptions { MaxDepth = 4 });
                var request = document.RootElement;
                if (request.ValueKind != JsonValueKind.Object || !request.TryGetProperty("id", out var requestId)
                    || (requestId.ValueKind != JsonValueKind.String && requestId.ValueKind != JsonValueKind.Number)
                    || requestId.GetRawText().Length > 128)
                    throw new ArgumentException("invalid_request");
                id = requestId.Clone();
                if (!request.TryGetProperty("args", out var arguments)
                    || arguments.ValueKind != JsonValueKind.Array || arguments.GetArrayLength() != 13)
                    throw new ArgumentException("invalid_arguments");
                var values = arguments.EnumerateArray().Select(value => value.ValueKind == JsonValueKind.String
                    ? value.GetString()! : throw new ArgumentException("invalid_arguments")).ToArray();
                var started = Stopwatch.StartNew();
                NativeRender.Render(RenderArguments(values));
                Send(protocol, new { protocol = 1, id, ok = true,
                    process_id = Environment.ProcessId, native_render_ms = started.Elapsed.TotalMilliseconds });
            }
            catch (Exception error)
            {
                // Source/cache cleanup and native process restart are owned by the bridge.
                Console.Error.WriteLine($"ESPER resident: {error.GetType().Name}: {error.Message}");
                Send(protocol, new { protocol = 1, id = id.ValueKind == JsonValueKind.Undefined ? (object?)null : id,
                    ok = false, error = error.GetType().Name, process_id = Environment.ProcessId });
            }
        }
        return 0;
    }
}
