// bairdcap - native capture helper of Baird's Hoard.
//
// Pulls the hardware-encoded MPEG-2 transport stream (H.264 video on PID 0x44,
// AAC audio on PID 0x45) out of an AVerMedia Live Gamer Portable family device
// (GL310 "LGP Lite" = C835, C875) and writes it to stdout untouched.
//
// Graph: crossbar -> "AVerMedia C835 Capture" (KS) -> "AVerMedia C835 H264
// Encoder" (KS, joined by medium) -> Sample Grabber -> Null Renderer.
// The encoder bridge pin only instantiates against its own encoder filter,
// which is why ordinary DirectShow clients (ffmpeg's dshow, browsers) cannot
// open the device.
//
// Encoder settings use the KS property set AVerMedia published for third-party
// software (AVER_HW_ENCODE_PROPERTY, MIT licensed header).
//
// Usage:
//   bairdcap.exe --list
//   bairdcap.exe [--device C835] [--out file|-] [--seconds N]
//                [--bitrate KBPS] [--fps N] [--size WxH] [--gop N]
//
// stdin, one command per line: "iframe" (insert a key frame now),
//   "bitrate N", "quit".
// stderr, one JSON object per line: {"event":"ready"|"stats"|"signal"|"error"|"done", ...}
//
// Exit codes: 0 ok, 2 device not found, 3 graph build failed, 4 no data,
//             5 device lost while running.
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using DirectShowLib;

[StructLayout(LayoutKind.Sequential)]
struct AverParameters
{
    public uint Index, Param1, Param2, Param3;
}

class Grabber : ISampleGrabberCB
{
    public Stream Sink;
    public long Bytes, Samples, LastTick;
    byte[] buf = new byte[1 << 20];
    readonly object gate = new object();

    public int SampleCB(double t, IMediaSample s) { return 0; }

    public int BufferCB(double t, IntPtr p, int len)
    {
        if (len <= 0) return 0;
        lock (gate)
        {
            if (buf.Length < len) buf = new byte[len];
            Marshal.Copy(p, buf, 0, len);
            Bytes += len; Samples++; LastTick = Environment.TickCount;
            if (Sink != null)
            {
                try { Sink.Write(buf, 0, len); Sink.Flush(); }
                catch (IOException) { Program.Stop.Set(); }
                catch (ObjectDisposedException) { Program.Stop.Set(); }
            }
        }
        return 0;
    }
}

static class Program
{
    public static readonly ManualResetEvent Stop = new ManualResetEvent(false);
    static readonly Guid AverHwEncodeProperty = new Guid(0x1bd55918, 0xbaf5, 0x4781, 0x8d, 0x76, 0xe0, 0xa0, 0xa5, 0xe1, 0xd2, 0xb8);
    const uint ParamFrameRate = 0, ParamBitRate = 1, ParamCurrentResolution = 2, ParamEncodeResolution = 3, ParamGop = 4, ParamInsertIFrame = 6;
    static readonly Guid KsCategoryEncoder = new Guid("19689BF6-C384-48fd-AD51-90E58C79F70B");

    static readonly object logGate = new object();
    static void Emit(string evt, string fields)
    {
        lock (logGate)
        {
            Console.Error.WriteLine("{\"event\":\"" + evt + "\"" + (string.IsNullOrEmpty(fields) ? "" : "," + fields) + "}");
            Console.Error.Flush();
        }
    }
    static string J(string s) { return "\"" + (s ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"") + "\""; }

    static IKsPropertySet[] propTargets = new IKsPropertySet[0];

    static int SetParam(uint index, uint p1, uint p2)
    {
        var data = new AverParameters { Index = index, Param1 = p1, Param2 = p2 };
        int size = Marshal.SizeOf(typeof(AverParameters));
        IntPtr ptr = Marshal.AllocCoTaskMem(size);
        try
        {
            Marshal.StructureToPtr(data, ptr, false);
            int last = -1;
            foreach (var ps in propTargets)
            {
                last = ps.Set(AverHwEncodeProperty, 0, ptr, size, ptr, size);
                if (last >= 0) return last;
            }
            return last;
        }
        finally { Marshal.FreeCoTaskMem(ptr); }
    }

    static bool GetParam(uint index, out uint p1, out uint p2)
    {
        p1 = p2 = 0;
        var data = new AverParameters { Index = index };
        int size = Marshal.SizeOf(typeof(AverParameters));
        IntPtr ptr = Marshal.AllocCoTaskMem(size);
        try
        {
            foreach (var ps in propTargets)
            {
                Marshal.StructureToPtr(data, ptr, false);
                int ret;
                if (ps.Get(AverHwEncodeProperty, 0, ptr, size, ptr, size, out ret) >= 0)
                {
                    var o = (AverParameters)Marshal.PtrToStructure(ptr, typeof(AverParameters));
                    p1 = o.Param1; p2 = o.Param2;
                    return true;
                }
            }
            return false;
        }
        finally { Marshal.FreeCoTaskMem(ptr); }
    }

    static DsDevice Find(Guid category, string match)
    {
        foreach (var d in DsDevice.GetDevicesOfCat(category))
            if (d.Name.IndexOf(match, StringComparison.OrdinalIgnoreCase) >= 0) return d;
        return null;
    }

    static bool HasStream(IPin pin)
    {
        IEnumMediaTypes et; if (pin.EnumMediaTypes(out et) != 0 || et == null) return false;
        var arr = new AMMediaType[1]; bool found = false;
        while (et.Next(1, arr, IntPtr.Zero) == 0)
        {
            if (arr[0].majorType == MediaType.Stream) found = true;
            DsUtils.FreeAMMediaType(arr[0]);
        }
        return found;
    }

    public static string Xbar = "";

    // Route HDMI video (Video_SerialDigital) to the video decoder output and
    // the audio input that belongs to it (its related pin) to the audio output.
    // audioPref: "hdmi" (default, the related pin), "line" or an input index.
    static void SelectHdmi(IAMCrossbar x, string audioPref)
    {
        int outs, ins; if (x.get_PinCounts(out outs, out ins) != 0) return;
        var sb = new StringBuilder();
        int hdmiVideo = -1, hdmiAudio = -1;
        for (int i = 0; i < ins; i++)
        {
            int rel; PhysicalConnectorType it; x.get_CrossbarPinInfo(true, i, out rel, out it);
            sb.Append("{\"in\":" + i + ",\"type\":\"" + it + "\",\"related\":" + rel + "},");
            if (it == PhysicalConnectorType.Video_SerialDigital && hdmiVideo < 0) { hdmiVideo = i; hdmiAudio = rel; }
        }
        for (int o = 0; o < outs; o++)
        {
            int rel; PhysicalConnectorType ot; x.get_CrossbarPinInfo(false, o, out rel, out ot);
            int want = -1;
            if (ot == PhysicalConnectorType.Video_VideoDecoder) want = hdmiVideo;
            else if (ot == PhysicalConnectorType.Audio_AudioDecoder)
            {
                int n;
                if (audioPref == "hdmi") want = hdmiAudio;
                else if (int.TryParse(audioPref, out n)) want = n;
                else for (int i = 0; i < ins; i++)
                {
                    int r2; PhysicalConnectorType it; x.get_CrossbarPinInfo(true, i, out r2, out it);
                    if (it == PhysicalConnectorType.Audio_Line && x.CanRoute(o, i) == 0) { want = i; break; }
                }
            }
            int hr = want >= 0 && x.CanRoute(o, want) == 0 ? x.Route(o, want) : -1;
            int cur; x.get_IsRoutedTo(o, out cur);
            sb.Append("{\"out\":" + o + ",\"type\":\"" + ot + "\",\"routed\":" + cur + ",\"hr\":" + hr + "},");
        }
        Xbar = "[" + sb.ToString().TrimEnd(',') + "]";
    }

    [MTAThread]
    static int Main(string[] args)
    {
        string device = "C835", outPath = "-", size = null, audio = "hdmi";
        int seconds = 0, bitrate = 0, fps = 0, gop = 0;
        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--list":
                    foreach (var d in DsDevice.GetDevicesOfCat(FilterCategory.VideoInputDevice)) Console.WriteLine(d.Name);
                    return 0;
                case "--device": device = args[++i]; break;
                case "--seconds": seconds = int.Parse(args[++i]); break;
                case "--out": outPath = args[++i]; break;
                case "--bitrate": bitrate = int.Parse(args[++i]); break;
                case "--fps": fps = int.Parse(args[++i]); break;
                case "--size": size = args[++i]; break;
                case "--gop": gop = int.Parse(args[++i]); break;
                case "--audio": audio = args[++i]; break;
            }
        }

        var dev = Find(FilterCategory.VideoInputDevice, device);
        if (dev == null) { Emit("error", "\"code\":\"device_not_found\",\"message\":" + J("No capture device matching " + device)); return 2; }

        var graph = (IGraphBuilder)new FilterGraph();
        var cgb = (ICaptureGraphBuilder2)new CaptureGraphBuilder2();
        IBaseFilter cap = null, enc = null;
        var cb = new Grabber();
        string encName = "";
        try
        {
            DsError.ThrowExceptionForHR(cgb.SetFiltergraph(graph));
            DsError.ThrowExceptionForHR(((IFilterGraph2)graph).AddSourceFilterForMoniker(dev.Mon, null, dev.Name, out cap));

            object xo;
            if (cgb.FindInterface(FindDirection.UpstreamOnly, null, cap, typeof(IAMCrossbar).GUID, out xo) >= 0 && xo != null)
                SelectHdmi((IAMCrossbar)xo, audio);

            IPin streamPin = null;
            IEnumPins ep; cap.EnumPins(out ep);
            var pins = new IPin[1];
            while (ep.Next(1, pins, IntPtr.Zero) == 0)
            {
                PinInfo pi; pins[0].QueryPinInfo(out pi);
                if (pi.dir == PinDirection.Output && streamPin == null && HasStream(pins[0])) streamPin = pins[0];
                DsUtils.FreePinInfo(pi);
            }
            if (streamPin == null) throw new Exception("the device exposes no encoder pin");

            IPin encOut = null;
            foreach (var d in DsDevice.GetDevicesOfCat(KsCategoryEncoder))
            {
                if (encOut != null) break;
                object eo; Guid ibf = typeof(IBaseFilter).GUID;
                try { d.Mon.BindToObject(null, null, ref ibf, out eo); } catch { continue; }
                var ef = (IBaseFilter)eo;
                if (graph.AddFilter(ef, d.Name) < 0) continue;
                IPin ein = DsFindPin.ByDirection(ef, PinDirection.Input, 0);
                if (ein != null && graph.ConnectDirect(streamPin, ein, null) >= 0)
                {
                    enc = ef; encName = d.Name;
                    encOut = DsFindPin.ByDirection(ef, PinDirection.Output, 0);
                }
                else graph.RemoveFilter(ef);
            }
            if (encOut == null) throw new Exception("no AVerMedia encoder filter accepted the device");

            var sgf = (IBaseFilter)new SampleGrabber();
            var sg = (ISampleGrabber)sgf;
            var mt = new AMMediaType { majorType = MediaType.Stream, subType = MediaSubType.Mpeg2Transport };
            DsError.ThrowExceptionForHR(sg.SetMediaType(mt));
            DsUtils.FreeAMMediaType(mt);
            sg.SetBufferSamples(false); sg.SetOneShot(false);
            DsError.ThrowExceptionForHR(sg.SetCallback(cb, 1));
            DsError.ThrowExceptionForHR(graph.AddFilter(sgf, "Grabber"));
            var nul = (IBaseFilter)new NullRenderer();
            DsError.ThrowExceptionForHR(graph.AddFilter(nul, "Null"));
            DsError.ThrowExceptionForHR(graph.Connect(encOut, DsFindPin.ByDirection(sgf, PinDirection.Input, 0)));
            DsError.ThrowExceptionForHR(graph.Connect(DsFindPin.ByDirection(sgf, PinDirection.Output, 0), DsFindPin.ByDirection(nul, PinDirection.Input, 0)));
        }
        catch (Exception e)
        {
            Emit("error", "\"code\":\"graph_failed\",\"message\":" + J(e.Message));
            return 3;
        }

        // The vendor property set may live on the encoder or on the capture filter.
        var targets = new System.Collections.Generic.List<IKsPropertySet>();
        if (enc is IKsPropertySet) targets.Add((IKsPropertySet)enc);
        if (cap is IKsPropertySet) targets.Add((IKsPropertySet)cap);
        propTargets = targets.ToArray();

        var applied = new StringBuilder();
        if (fps > 0) applied.Append(",\"fps\":" + SetParam(ParamFrameRate, (uint)fps, 0));
        if (bitrate > 0) applied.Append(",\"bitrate\":" + SetParam(ParamBitRate, (uint)bitrate, 0));
        if (!string.IsNullOrEmpty(size) && size.Contains("x"))
        {
            var wh = size.Split('x');
            applied.Append(",\"size\":" + SetParam(ParamEncodeResolution, uint.Parse(wh[0]), uint.Parse(wh[1])));
        }
        if (gop > 0) applied.Append(",\"gop\":" + SetParam(ParamGop, (uint)gop, 0));

        cb.Sink = outPath == "-" ? Console.OpenStandardOutput() : new FileStream(outPath, FileMode.Create, FileAccess.Write, FileShare.Read);

        var mc = (IMediaControl)graph;
        int rhr = mc.Run();
        if (rhr < 0) { Emit("error", "\"code\":\"run_failed\",\"message\":\"IMediaControl.Run 0x" + rhr.ToString("X8") + "\""); return 3; }

        uint w, h, cur;
        string input = GetParam(ParamCurrentResolution, out w, out h) ? ",\"input\":{\"width\":" + w + ",\"height\":" + h + "}" : "";
        string fpsNow = GetParam(ParamFrameRate, out cur, out h) ? ",\"fps\":" + cur : "";
        string brNow = GetParam(ParamBitRate, out cur, out h) ? ",\"bitrate_kbps\":" + cur : "";
        Emit("ready", "\"device\":" + J(dev.Name) + ",\"encoder\":" + J(encName) + input + fpsNow + brNow + (Xbar.Length > 0 ? ",\"crossbar\":" + Xbar : "") + (applied.Length > 0 ? ",\"applied\":{" + applied.ToString(1, applied.Length - 1) + "}" : ""));

        new Thread(() =>
        {
            try
            {
                var reader = new StreamReader(Console.OpenStandardInput());
                string line;
                while ((line = reader.ReadLine()) != null)
                {
                    line = line.Trim();
                    if (line == "quit") break;
                    if (line == "iframe") Emit("iframe", "\"hr\":" + SetParam(ParamInsertIFrame, 0, 0));
                    else if (line.StartsWith("bitrate ")) Emit("bitrate", "\"hr\":" + SetParam(ParamBitRate, uint.Parse(line.Substring(8)), 0));
                }
            }
            catch { }
            Stop.Set();
        }) { IsBackground = true }.Start();

        int waited = 0, code = 0; long lastBytes = 0; string lastInput = input;
        cb.LastTick = Environment.TickCount;
        while (!Stop.WaitOne(1000))
        {
            waited++;
            if (waited % 2 == 0)
            {
                Emit("stats", "\"seconds\":" + waited + ",\"bytes\":" + cb.Bytes + ",\"kbps\":" + ((cb.Bytes - lastBytes) * 8 / 2000));
                lastBytes = cb.Bytes;
                string now = GetParam(ParamCurrentResolution, out w, out h) ? ",\"input\":{\"width\":" + w + ",\"height\":" + h + "}" : "";
                if (now != lastInput) { Emit("signal", now.TrimStart(',')); lastInput = now; }
            }
            if (seconds > 0 && waited >= seconds) break;
            if (Environment.TickCount - cb.LastTick > 8000)
            {
                Emit("error", "\"code\":\"no_data\",\"message\":\"The device stopped sending data for 8 seconds (signal lost or unplugged).\"");
                code = cb.Bytes > 0 ? 5 : 4;
                break;
            }
        }
        try { mc.Stop(); } catch { }
        try { if (cb.Sink != null) cb.Sink.Dispose(); } catch { }
        Emit("done", "\"bytes\":" + cb.Bytes);
        Marshal.ReleaseComObject(graph);
        return code != 0 ? code : (cb.Bytes > 0 ? 0 : 4);
    }
}
