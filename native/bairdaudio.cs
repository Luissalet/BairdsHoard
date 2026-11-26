// WASAPI shared-mode loopback and optional capture device. PCM on stdout, levels on stderr.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
class MMDeviceEnumerator { }

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("A95664D2-9614-4F35-A746-DE8DB63617E6")]
interface IMMDeviceEnumerator
{
    [PreserveSig] int EnumAudioEndpoints(int flow, uint mask, out IntPtr devices);
    [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice device);
}

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("D666063F-1587-4E43-81F1-B948E807363F")]
interface IMMDevice
{
    [PreserveSig] int Activate(ref Guid iid, uint context, IntPtr activation, [MarshalAs(UnmanagedType.IUnknown)] out object instance);
}

[StructLayout(LayoutKind.Sequential, Pack = 2)]
struct WaveFormat
{
    public ushort Tag, Channels;
    public uint SamplesPerSecond, BytesPerSecond;
    public ushort BlockAlign, BitsPerSample, ExtraSize;
}

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")]
interface IAudioClient
{
    [PreserveSig] int Initialize(int share, uint flags, long buffer, long period, ref WaveFormat format, IntPtr session);
    [PreserveSig] int GetBufferSize(out uint frames);
    [PreserveSig] int GetStreamLatency(out long latency);
    [PreserveSig] int GetCurrentPadding(out uint frames);
    [PreserveSig] int IsFormatSupported(int share, IntPtr format, out IntPtr closest);
    [PreserveSig] int GetMixFormat(out IntPtr format);
    [PreserveSig] int GetDevicePeriod(out long normal, out long minimum);
    [PreserveSig] int Start();
    [PreserveSig] int Stop();
    [PreserveSig] int Reset();
    [PreserveSig] int SetEventHandle(IntPtr handle);
    [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
}

[ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317")]
interface IAudioCaptureClient
{
    [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong position, out ulong qpc);
    [PreserveSig] int ReleaseBuffer(uint frames);
    [PreserveSig] int GetNextPacketSize(out uint frames);
}

sealed class CapturedAudio : IDisposable
{
    const uint Loopback = 0x20000, AutoConvert = 0x80000000, SrcQuality = 0x08000000;
    const uint Silent = 2, TimestampError = 4;
    const int Rate = 48000;

    sealed class Packet
    {
        public long Start;
        public short[] Samples;
        public long End { get { return Start + Samples.Length / 2; } }
    }

    readonly Queue<Packet> packets = new Queue<Packet>();
    IMMDevice endpoint;
    IAudioClient client;
    IAudioCaptureClient capture;
    bool started;
    long lastEnd = long.MinValue;

    public CapturedAudio(IMMDeviceEnumerator devices, bool loopback)
    {
        try
        {
            // eRender/eCapture, eConsole. There is no render or volume client in this process.
            Check(devices.GetDefaultAudioEndpoint(loopback ? 0 : 1, 0, out endpoint));
            Guid iid = typeof(IAudioClient).GUID;
            object instance;
            Check(endpoint.Activate(ref iid, 23, IntPtr.Zero, out instance));
            client = (IAudioClient)instance;
            var format = new WaveFormat {
                Tag = 1, Channels = 2, SamplesPerSecond = Rate, BytesPerSecond = Rate * 4,
                BlockAlign = 4, BitsPerSample = 16
            };
            Check(client.Initialize(0, AutoConvert | SrcQuality | (loopback ? Loopback : 0),
                                    1000000, 0, ref format, IntPtr.Zero));
            iid = typeof(IAudioCaptureClient).GUID;
            Check(client.GetService(ref iid, out instance));
            capture = (IAudioCaptureClient)instance;
        }
        catch
        {
            Dispose();
            throw;
        }
    }

    public void Start()
    {
        Check(client.Start());
        started = true;
    }

    public void Drain(long epoch, long now, long emitted)
    {
        uint size;
        while (true)
        {
            Check(capture.GetNextPacketSize(out size));
            if (size == 0)
                return;
            IntPtr data;
            uint frames, flags;
            ulong position, qpc;
            int result = capture.GetBuffer(out data, out frames, out flags, out position, out qpc);
            Check(result);
            if (frames == 0)
                return;
            try
            {
                long beginning = (flags & TimestampError) == 0
                    ? ((long)qpc - epoch) * Rate / 10000000
                    : Math.Max(lastEnd, now - frames);
                // Small device-clock differences do not open a gap between adjacent packets.
                if (lastEnd != long.MinValue && Math.Abs(beginning - lastEnd) < 24)
                    beginning = lastEnd;
                lastEnd = beginning + frames;
                if (lastEnd > emitted && (flags & Silent) == 0 && data != IntPtr.Zero)
                {
                    var samples = new short[checked((int)frames * 2)];
                    Marshal.Copy(data, samples, 0, samples.Length);
                    packets.Enqueue(new Packet { Start = beginning, Samples = samples });
                }
            }
            finally
            {
                Check(capture.ReleaseBuffer(frames));
            }
        }
    }

    public int Render(short[] output, long from, int frames, int channels, int channel)
    {
        long end = from + frames;
        int peak = 0;
        while (packets.Count != 0)
        {
            var packet = packets.Peek();
            if (packet.Start >= end)
                break;
            long first = Math.Max(from, packet.Start);
            long last = Math.Min(end, packet.End);
            for (long at = first; at < last; at++)
            {
                int src = (int)(at - packet.Start) * 2;
                int dst = (int)(at - from) * channels + channel;
                output[dst] = packet.Samples[src];
                output[dst + 1] = packet.Samples[src + 1];
                peak = Math.Max(peak, Math.Max(Math.Abs((int)output[dst]), Math.Abs((int)output[dst + 1])));
            }
            if (packet.End > end)
                break;
            packets.Dequeue();
        }
        return peak;
    }

    public void Dispose()
    {
        if (started) client.Stop();
        Release(capture);
        Release(client);
        Release(endpoint);
        started = false;
        capture = null;
        client = null;
        endpoint = null;
    }

    public static void Check(int hr)
    {
        if (hr < 0) Marshal.ThrowExceptionForHR(hr);
    }

    public static void Release(object instance)
    {
        if (instance != null) Marshal.ReleaseComObject(instance);
    }
}

static class BairdAudio
{
    const int Rate = 48000, Block = 480;
    const int JitterBuffer = Rate / 20;

    static long Clock100ns()
    {
        long ticks = Stopwatch.GetTimestamp();
        return (ticks / Stopwatch.Frequency) * 10000000
            + (ticks % Stopwatch.Frequency) * 10000000 / Stopwatch.Frequency;
    }

    static string Level(int value)
    {
        return (value / 32768.0).ToString("0.0000", CultureInfo.InvariantCulture);
    }

    static void Run(CapturedAudio loop, CapturedAudio mic, Stream sink)
    {
        int channels = mic == null ? 2 : 4;
        var samples = new short[Block * channels];
        var bytes = new byte[samples.Length * 2];
        long epoch = Clock100ns(), emitted = 0, lastReport = 0;
        int peak = 0, micPeak = 0;
        loop.Start();
        if (mic != null) mic.Start();
        while (true)
        {
            long now = (Clock100ns() - epoch) * Rate / 10000000;
            loop.Drain(epoch, now, emitted);
            if (mic != null) mic.Drain(epoch, now, emitted);
            while (emitted + Block <= now - JitterBuffer)
            {
                Array.Clear(samples, 0, samples.Length);
                peak = Math.Max(peak, loop.Render(samples, emitted, Block, channels, 0));
                if (mic != null)
                    micPeak = Math.Max(micPeak, mic.Render(samples, emitted, Block, channels, 2));
                Buffer.BlockCopy(samples, 0, bytes, 0, bytes.Length);
                sink.Write(bytes, 0, bytes.Length);
                emitted += Block;
            }
            if (emitted - lastReport >= Rate / 4)
            {
                Console.Error.WriteLine("{\"event\":\"levels\",\"audio_level\":" + Level(peak)
                    + ",\"microphone_level\":" + (mic == null ? "null" : Level(micPeak)) + "}");
                lastReport = emitted;
                peak = micPeak = 0;
            }
            Thread.Sleep(4);
        }
    }

    [MTAThread]
    public static int Main(string[] args)
    {
        bool microphone = args.Length == 1 && args[0] == "--microphone";
        if (args.Length != 0 && !microphone)
            return 2;
        IMMDeviceEnumerator devices = null;
        try
        {
            devices = (IMMDeviceEnumerator)new MMDeviceEnumerator();
            using (var system = new CapturedAudio(devices, true))
            using (var mic = microphone ? new CapturedAudio(devices, false) : null)
            using (var sink = Console.OpenStandardOutput())
                Run(system, mic, sink);
            return 0;
        }
        catch (IOException) { return 0; }  // The encoder closed the PCM pipe.
        catch (Exception error)
        {
            Console.Error.WriteLine("WASAPI capture failed: " + error.Message.Replace('\r', ' ').Replace('\n', ' '));
            return 1;
        }
        finally { CapturedAudio.Release(devices); }
    }
}
