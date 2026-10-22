// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Zen-harness.exe: starts Zen harness on this computer and opens it in a window of its own.
//
// The folder it sits in (desktop/build.py makes it):
//     Zen-harness.exe  this launcher
//     settings.env     Zen's Model settings - where the model runs, its address and key - and any other setting
//     runtime\         Python with Zen's libraries (the code sandbox runs scripts with this Python too)
//     zen\             app\ and web\
//     tools\           node and Playwright MCP: the browser Zen reads blocked pages with and tries its games in
//     data\            projects, chats and files - everything that is yours; logs\ holds the servers' output
//
// One click: the app, then the window - Zen's own (WebView2, its page drawing the title bar), or without WebView2 Edge's
// app mode. Closing the window stops the app. A second click while Zen runs opens another window. The app reaches the
// model itself - the Kaggle notebook, a llama-server here or a cloud - as its Model settings say (set in the app; the
// first time, they open by themselves).
// Built by desktop/build.py with the C# compiler every Windows has (.NET Framework 4): C# 5, and Microsoft's WebView2 SDK
// (desktop/webview2: its three files go beside the exe).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Net;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Win32;

static class Zen
{
    const string App = "http://127.0.0.1:8000/";
    // Zen's app is up: its tool names answer at once (its status asks the model first - over Kaggle's tunnel, seconds)
    const string Up = App + "assistant/tools";
    static readonly string Root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
    static readonly string SettingsFile = Path.Combine(Root, "settings.env");
    static readonly string Data = Path.Combine(Root, "data");
    static readonly string Logs = Path.Combine(Data, "logs");
    static readonly List<Process> started = new List<Process>();

    [STAThread]
    static int Main()
    {
        SetProcessDPIAware();  // sharp on a scaled screen: the windows below size themselves with Px
        using (var screen = Graphics.FromHwnd(IntPtr.Zero)) Scale = screen.DpiX / 96f;
        families = LoadZenType();
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        if (Answers(Up, 3000))  // already running: one more window
        {
            var another = OwnWindow();
            if (another != null) Application.Run(another);
            else OpenWindow();
            return 0;
        }
        Directory.CreateDirectory(Logs);
        var splash = new Splash();
        try
        {
            var settings = ReadSettings();
            splash.Say("Starting Zen…");
            var app = Start(Python(), "-m uvicorn app.main:app --host 127.0.0.1 --port 8000", "zen.log", settings);
            if (!WaitFor(Up, 120, app))
            {
                splash.Hide();
                Fail("Zen didn't start.", Path.Combine(Logs, "zen.log"));
                return 1;
            }
            splash.Say("Opening Zen…");
            var own = OwnWindow();
            if (own != null)
            {
                splash.Hide();
                Application.Run(own);
                return 0;
            }
            var window = OpenWindow();
            if (window == null)
            {
                splash.Hide();
                Application.Run(new RunningForm());
                return 0;
            }
            // the splash stays until Edge's window is there, so nothing blinks out in between
            var end = DateTime.Now.AddSeconds(10);
            while (DateTime.Now < end && !window.HasExited)
            {
                window.Refresh();
                if (window.MainWindowHandle != IntPtr.Zero) { Pump(400); break; }
                Pump(50);
            }
            splash.Hide();
            window.WaitForExit();
            return 0;
        }
        finally
        {
            splash.Dispose();
            StopAll();
        }
    }

    static string Python() { return Path.Combine(Root, "runtime", "python.exe"); }

    static Process Start(string exe, string args, string log, Dictionary<string, string> settings)
    {
        var info = new ProcessStartInfo(exe, args)
        {
            WorkingDirectory = Path.Combine(Root, "zen"),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        var env = info.EnvironmentVariables;
        foreach (var pair in settings) env[pair.Key] = pair.Value;
        // the user's own things live in data\, next to this launcher; the browser tool's node comes with it
        env["ASSISTANT_DATA_DIR"] = Path.Combine(Data, "assistant");
        // the file the app's Model settings are in: written from the app, read again by it when it changes
        env["ZEN_MODEL_SETTINGS"] = SettingsFile;
        string mcp = Path.Combine(Root, "tools", "playwright-mcp");
        if (Directory.Exists(mcp) && string.IsNullOrEmpty(env["ASSISTANT_PLAYWRIGHT_MCP"])) env["ASSISTANT_PLAYWRIGHT_MCP"] = mcp;
        env["PATH"] = Path.Combine(Root, "tools", "node") + ";" + Path.Combine(Root, "runtime") + ";" + env["PATH"];
        env["PYTHONUTF8"] = "1";
        env["PYTHONUNBUFFERED"] = "1";  // each log line as it happens (piped, Python would hold them back)
        var writer = new StreamWriter(Path.Combine(Logs, log), false, new UTF8Encoding(false)) { AutoFlush = true };
        var proc = new Process { StartInfo = info };
        DataReceivedEventHandler keep = (s, e) => { if (e.Data != null) lock (writer) writer.WriteLine(e.Data); };
        proc.OutputDataReceived += keep;
        proc.ErrorDataReceived += keep;
        proc.Start();
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        started.Add(proc);
        return proc;
    }

    // Until url answers, the process exits or the time is up - asked off this thread, so the splash keeps moving
    static bool WaitFor(string url, int seconds, Process proc)
    {
        var end = DateTime.Now.AddSeconds(seconds);
        while (DateTime.Now < end)
        {
            var ask = Task.Factory.StartNew(() => Answers(url, 3000));
            while (!ask.IsCompleted) Pump(15);
            if (ask.Result) return true;
            if (proc.HasExited) return false;
            Pump(400);
        }
        return false;
    }

    // The windows kept painting for that long (the splash's bar, the moon's light)
    static void Pump(int ms)
    {
        var end = DateTime.Now.AddMilliseconds(ms);
        do { Application.DoEvents(); Thread.Sleep(10); } while (DateTime.Now < end);
    }

    static bool Answers(string url, int ms)
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Timeout = ms;
            req.Proxy = null;
            using (var res = (HttpWebResponse)req.GetResponse()) return (int)res.StatusCode < 400;
        }
        catch (Exception) { return false; }
    }

    // Zen's own window, its page loaded (the splash keeps moving meanwhile); null without WebView2 (Windows 11 has it,
    // Edge installs it on Windows 10) or without its files beside this launcher
    static Form OwnWindow()
    {
        try { return LoadOwnWindow(); }
        catch (Exception) { return null; }  // FileNotFoundException (the SDK's files), WebView2RuntimeNotFoundException...
    }

    [MethodImpl(MethodImplOptions.NoInlining)]  // compiled at its call, so missing SDK files throw inside the try above
    static Form LoadOwnWindow()
    {
        CoreWebView2Environment.GetAvailableBrowserVersionString();  // throws when there is no WebView2
        // WebView2 resumes its start on this thread, through WinForms' way back to it
        if (!(SynchronizationContext.Current is WindowsFormsSynchronizationContext))
            SynchronizationContext.SetSynchronizationContext(new WindowsFormsSynchronizationContext());
        var window = new ZenWindow(App, Data);
        var loaded = window.Open();
        var end = DateTime.Now.AddSeconds(40);
        while (!loaded.IsCompleted && DateTime.Now < end) Pump(15);
        if (loaded.IsCompleted && loaded.Result) return window;
        window.Dispose();
        return null;
    }

    // Edge's app mode, a profile of its own (a window, not a tab among the user's): its process lasts until the window
    // is closed. Without Edge, the default browser, and null.
    static Process OpenWindow()
    {
        string edge = FindEdge();
        if (edge == null)
        {
            Process.Start(App);
            return null;
        }
        var profile = Path.Combine(Data, "window");
        // no syncing the user's Edge data (favourites, history, passwords) into Zen's window
        var args = "--app=" + App + " --user-data-dir=\"" + profile + "\" --no-first-run --no-default-browser-check " +
                   "--disable-sync --disable-features=msEdgeSidebarV2";
        // the window draws its own title bar, dark or light only as it opens - dark, as Zen's page is, unless light was
        // picked in it (the page tells the app, which keeps it here)
        string theme = "";
        try { theme = File.ReadAllText(Path.Combine(Data, "assistant", "window-theme")).Trim(); } catch (Exception) { }
        if (theme != "light") args += " --force-dark-mode";
        // 1320 x 880 where the screen has room, in the middle of it (Edge's sizes are 100 % pixels, the screen's real)
        var area = Screen.PrimaryScreen.WorkingArea;
        float aw = area.Width / Scale, ah = area.Height / Scale;
        int w = (int)Math.Min(1320, aw - 64), h = (int)Math.Min(880, ah - 48);
        args += " --window-size=" + w + "," + h + " --window-position=" + (int)(area.Left / Scale + (aw - w) / 2) + "," + (int)(area.Top / Scale + (ah - h) / 2);
        return Process.Start(new ProcessStartInfo(edge, args) { UseShellExecute = false });
    }

    static string FindEdge()
    {
        foreach (var hive in new[] { Registry.LocalMachine, Registry.CurrentUser })
        {
            using (var key = hive.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"))
            {
                var path = key == null ? null : key.GetValue("") as string;
                if (!string.IsNullOrEmpty(path) && File.Exists(path)) return path;
            }
        }
        foreach (var dir in new[] { Environment.GetEnvironmentVariable("ProgramFiles(x86)"), Environment.GetEnvironmentVariable("ProgramFiles") })
        {
            if (dir == null) continue;
            var path = Path.Combine(dir, @"Microsoft\Edge\Application\msedge.exe");
            if (File.Exists(path)) return path;
        }
        return null;
    }

    // The servers and everything they started (a script the sandbox runs, the browser tool's node)
    static void StopAll() { foreach (var p in started) Stop(p); }

    static void Stop(Process p)
    {
        if (p == null) return;
        try
        {
            if (p.HasExited) return;
            var kill = Process.Start(new ProcessStartInfo("taskkill", "/T /F /PID " + p.Id) { CreateNoWindow = true, UseShellExecute = false });
            if (kill != null) kill.WaitForExit(5000);
        }
        catch (Exception) { }
    }

    static Dictionary<string, string> ReadSettings()
    {
        var values = new Dictionary<string, string>();
        if (!File.Exists(SettingsFile)) return values;
        foreach (var raw in File.ReadAllLines(SettingsFile, Encoding.UTF8))
        {
            var line = raw.Trim();
            int eq = line.IndexOf('=');
            if (line.Length == 0 || line.StartsWith("#") || eq < 1) continue;
            var value = line.Substring(eq + 1).Trim();
            if (value.Length >= 2 && (value[0] == '"' || value[0] == '\'') && value[value.Length - 1] == value[0]) value = value.Substring(1, value.Length - 2);
            values[line.Substring(0, eq).Trim()] = value;
        }
        return values;
    }

    static void Fail(string what, string log)
    {
        string tail = "";
        try
        {
            string[] lines;  // the server may still be writing it: read alongside
            using (var file = new FileStream(log, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var text = new StreamReader(file, Encoding.UTF8)) lines = text.ReadToEnd().Split('\n');
            tail = string.Join("\n", lines, Math.Max(0, lines.Length - 12), Math.Min(12, lines.Length));
        }
        catch (Exception) { }
        MessageBox.Show(what + "\n\nThe last lines of " + log + ":\n\n" + tail, "Zen harness", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    // Zen's own colours (web/app.css, dark): the page, a raised surface, the text, the quieter texts, the line between
    // things, and the moon's two ends and its middle
    internal static readonly Color Bg = Color.FromArgb(0x1c, 0x1c, 0x1c);
    internal static readonly Color Surface = Color.FromArgb(0x2a, 0x2a, 0x2a);
    internal static readonly Color Ink = Color.FromArgb(0xec, 0xec, 0xec);
    internal static readonly Color Muted = Color.FromArgb(0xb4, 0xb4, 0xb4);
    internal static readonly Color Soft = Color.FromArgb(0x8e, 0x8e, 0x8e);
    internal static readonly Color Line = Color.FromArgb(0x38, 0x38, 0x38);
    internal static readonly Color MoonA = Color.FromArgb(0xb9, 0xad, 0xff);
    internal static readonly Color MoonB = Color.FromArgb(0x5a, 0x47, 0xe0);
    internal static readonly Color Moon = Color.FromArgb(0x8e, 0x7d, 0xf5);

    // The screen's scale (1 at 100 %, 1.5 at 150 %): every size below is in 100 % pixels, through Px
    internal static float Scale = 1;
    internal static int Px(float v) { return (int)Math.Round(v * Scale); }

    // Zen's own type, inside this exe (desktop/fonts: Google Sans Flex as the page sets it, made into Zen Sans by
    // desktop/make_fonts.py), loaded for both of Windows' text drawers - GDI+ (a Font's family) and GDI (labels and
    // TextRenderer, which find it by name); without it, Windows 11's Segoe UI Variable, else Segoe UI (Windows 10)
    // (loaded by Main once the process is DPI aware: drawing anything earlier would lock it into Windows' stretching)
    static PrivateFontCollection zenType;
    static Dictionary<string, FontFamily> families = new Dictionary<string, FontFamily>();

    static Dictionary<string, FontFamily> LoadZenType()
    {
        var found = new Dictionary<string, FontFamily>();
        try
        {
            zenType = new PrivateFontCollection();
            foreach (var file in new[] { "ZenSans-Regular.ttf", "ZenSans-SemiBold.ttf" })
            {
                byte[] data;
                using (var stream = typeof(Zen).Assembly.GetManifestResourceStream(file))
                {
                    if (stream == null) return found;
                    data = new byte[stream.Length];
                    stream.Read(data, 0, data.Length);
                }
                var memory = Marshal.AllocCoTaskMem(data.Length);  // kept while Zen runs: both drawers read it
                Marshal.Copy(data, 0, memory, data.Length);
                zenType.AddMemoryFont(memory, data.Length);
                uint count = 0;
                AddFontMemResourceEx(memory, (uint)data.Length, IntPtr.Zero, ref count);
            }
            foreach (var family in zenType.Families) found[family.Name] = family;
        }
        catch (Exception) { }
        return found;
    }

    [DllImport("gdi32.dll")]
    static extern IntPtr AddFontMemResourceEx(IntPtr font, uint size, IntPtr reserved, ref uint fonts);

    static readonly Dictionary<string, bool> fonts = new Dictionary<string, bool>();
    static string FirstFont(string wanted, string fallback)
    {
        bool have;
        if (!fonts.TryGetValue(wanted, out have))
        {
            try { using (new FontFamily(wanted)) have = true; } catch (ArgumentException) { have = false; }
            fonts[wanted] = have;
        }
        return have ? wanted : fallback;
    }
    internal static Font UiFont(float size, bool strong = false)
    {
        FontFamily zen;
        if (families.TryGetValue(strong ? "Zen Sans SemiBold" : "Zen Sans", out zen)) return new Font(zen, size);
        return new Font(strong ? FirstFont("Segoe UI Variable Text Semibold", "Segoe UI Semibold") : FirstFont("Segoe UI Variable Text", "Segoe UI"), size);
    }
    internal static Font TitleFont(float size, bool strong = true)
    {
        if (families.Count > 0) return UiFont(size, strong);
        // GDI keeps 31 letters of a name: the variable font's semibold is "...Display Semib"
        return new Font(strong ? FirstFont("Segoe UI Variable Display Semib", "Segoe UI Semibold") : FirstFont("Segoe UI Variable Display", "Segoe UI"), size);
    }

    internal static Icon AppIcon()
    {
        try { return Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch (Exception) { return null; }
    }

    // Zen's crescent (web/index.html's #mark): a disc of radius 9 at (12, 12) less the arc of radius 8.4 through the same
    // two points, lit lower right, in the moon's gradient - drawn, so it is sharp at any size. box is its 17.334 view box.
    internal static void DrawMark(Graphics g, RectangleF box)
    {
        float s = box.Width / 17.334f, o = 3.666f;
        using (var path = new GraphicsPath())
        {
            path.AddArc(box.X + (3 - o) * s, box.Y + (3 - o) * s, 18 * s, 18 * s, -67.812f, 225.624f);
            path.AddArc(box.X + (0.2029f - o) * s, box.Y + (0.2029f - o) * s, 16.8f * s, 16.8f * s, 125.996f, -161.991f);
            path.CloseFigure();
            using (var light = new LinearGradientBrush(new PointF(box.X - 1, box.Y - 1), new PointF(box.Right + 1, box.Bottom + 1), MoonA, MoonB))
                g.FillPath(light, path);
        }
    }

    // A soft round light of the moon's colour, strongest at its middle
    internal static void DrawGlow(Graphics g, PointF middle, float radius, int alpha)
    {
        using (var path = new GraphicsPath())
        {
            path.AddEllipse(middle.X - radius, middle.Y - radius, 2 * radius, 2 * radius);
            using (var light = new PathGradientBrush(path))
            {
                light.CenterColor = Color.FromArgb(alpha, Moon);
                light.SurroundColors = new[] { Color.FromArgb(0, Moon) };
                // fading out softly towards the edge (0 there, 1 at the middle), so no edge shows
                light.Blend = new Blend { Factors = new[] { 0f, 0.06f, 0.25f, 0.6f, 1f }, Positions = new[] { 0f, 0.25f, 0.5f, 0.75f, 1f } };
                g.FillPath(light, path);
            }
        }
    }

    internal static GraphicsPath Rounded(RectangleF r, float radius)
    {
        float d = Math.Min(2 * radius, Math.Min(r.Width, r.Height));
        var path = new GraphicsPath();
        path.AddArc(r.X, r.Y, d, d, 180, 90);
        path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    [DllImport("user32.dll")]
    static extern bool SetProcessDPIAware();

    [DllImport("user32.dll")]
    internal static extern IntPtr SendMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam);

    [DllImport("dwmapi.dll")]
    static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

    [StructLayout(LayoutKind.Sequential)]
    struct Margins { public int Left, Right, Top, Bottom; }

    [DllImport("dwmapi.dll")]
    static extern int DwmExtendFrameIntoClientArea(IntPtr hwnd, ref Margins margins);

    // A dark title bar over a dark window (Windows 10 1809 and later; elsewhere nothing changes)
    internal static void DarkTitle(Form form)
    {
        int on = 1;
        try { if (DwmSetWindowAttribute(form.Handle, 20, ref on, 4) != 0) DwmSetWindowAttribute(form.Handle, 19, ref on, 4); }
        catch (Exception) { }
    }

    // A window without Windows' frame that still sits on the desktop like one: the shadow under it, and on Windows 11
    // round corners and a hairline edge (elsewhere square corners)
    internal static void Float(Form form)
    {
        int on = 2, round = 2, edge = 0x383838;  // a COLORREF, 0x00bbggrr
        try
        {
            DwmSetWindowAttribute(form.Handle, 2, ref on, 4);      // DWMWA_NCRENDERING_POLICY enabled: the shadow
            var margins = new Margins { Left = 1, Right = 1, Top = 1, Bottom = 1 };
            DwmExtendFrameIntoClientArea(form.Handle, ref margins);
            DwmSetWindowAttribute(form.Handle, 33, ref round, 4);  // DWMWA_WINDOW_CORNER_PREFERENCE: round
            DwmSetWindowAttribute(form.Handle, 34, ref edge, 4);   // DWMWA_BORDER_COLOR
        }
        catch (Exception) { }
    }
}

// Zen's own windows: dark, without Windows' frame, moved by their background; with header, the crescent and the
// name small at the top left, where a title bar would be
class Floating : Form
{
    protected bool header;

    public Floating()
    {
        Text = "Zen harness";
        Icon = Zen.AppIcon();
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.None;
        BackColor = Zen.Bg;
        ForeColor = Zen.Ink;
        Font = Zen.UiFont(10f);
        DoubleBuffered = true;
    }

    protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); Zen.Float(this); }

    protected override void WndProc(ref Message m)
    {
        base.WndProc(ref m);
        if (m.Msg == 0x84 && (int)m.Result == 1) m.Result = (IntPtr)2;  // WM_NCHITTEST: the background is a title bar
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        if (!header) return;
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.PixelOffsetMode = PixelOffsetMode.HighQuality;
        Zen.DrawMark(g, new RectangleF(Zen.Px(20), Zen.Px(17), Zen.Px(15), Zen.Px(15)));
        using (var small = Zen.UiFont(9f))
            TextRenderer.DrawText(g, "Zen harness", small, new Point(Zen.Px(42), Zen.Px(15)), Zen.Soft, Zen.Bg);
    }
}

// While the servers come up: the crescent in its light, Zen's name, what is happening and a bar that keeps moving
class Splash : Floating
{
    readonly System.Windows.Forms.Timer tick = new System.Windows.Forms.Timer { Interval = 15 };
    readonly Stopwatch clock = Stopwatch.StartNew();
    readonly Font name = Zen.TitleFont(21f), name2 = Zen.TitleFont(21f, false), line = Zen.UiFont(10f);
    string status = "";

    public Splash()
    {
        ClientSize = new Size(Zen.Px(400), Zen.Px(260));
        tick.Tick += (s, e) => Invalidate();
    }

    // The line under the name
    public void Say(string text)
    {
        status = text;
        if (!Visible) Show();
        Invalidate();
        Update();
    }

    protected override void OnVisibleChanged(EventArgs e) { base.OnVisibleChanged(e); tick.Enabled = Visible; }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.PixelOffsetMode = PixelOffsetMode.HighQuality;
        double t = clock.Elapsed.TotalSeconds;
        float w = ClientSize.Width, size = Zen.Px(64), mid = Zen.Px(86);

        Zen.DrawGlow(g, new PointF(w / 2, mid), size * 1.3f, (int)(40 + 22 * Math.Sin(t * 2.2)));
        Zen.DrawMark(g, new RectangleF(w / 2 - size / 2, mid - size / 2, size, size));

        // "Zen harness": the name in Zen's ink, the rest quieter
        var flags = TextFormatFlags.NoPadding | TextFormatFlags.SingleLine;
        Size a = TextRenderer.MeasureText(g, "Zen", name, Size.Empty, flags), b = TextRenderer.MeasureText(g, " harness", name2, Size.Empty, flags);
        int x = (int)(w - a.Width - b.Width) / 2, y = Zen.Px(140);
        TextRenderer.DrawText(g, "Zen", name, new Point(x, y), Zen.Ink, Zen.Bg, flags);
        TextRenderer.DrawText(g, " harness", name2, new Point(x + a.Width, y), Zen.Soft, Zen.Bg, flags);

        TextRenderer.DrawText(g, status, line, new Rectangle(0, Zen.Px(186), (int)w, Zen.Px(22)), Zen.Muted, Zen.Bg,
                              TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine | TextFormatFlags.EndEllipsis);

        // the bar: a stretch of moonlight that eases across a dark track, again and again
        var track = new RectangleF((w - Zen.Px(160)) / 2, Zen.Px(224), Zen.Px(160), Zen.Px(3));
        using (var path = Zen.Rounded(track, track.Height / 2))
        {
            using (var dark = new SolidBrush(Zen.Surface)) g.FillPath(dark, path);
            double p = t % 1.5 / 1.5, eased = p < 0.5 ? 2 * p * p : 1 - Math.Pow(-2 * p + 2, 2) / 2;
            float run = track.Width * 0.4f, from = track.X - run + (float)eased * (track.Width + run);
            g.SetClip(path);
            using (var lit = Zen.Rounded(new RectangleF(from, track.Y, run, track.Height), track.Height / 2))
            using (var light = new LinearGradientBrush(new PointF(from - 1, 0), new PointF(from + run + 1, 0), Zen.MoonA, Zen.MoonB))
                g.FillPath(light, lit);
            g.ResetClip();
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { tick.Dispose(); name.Dispose(); name2.Dispose(); line.Dispose(); }
        base.Dispose(disposing);
    }
}

class Pill : Button
{
    readonly bool main;
    bool hover, down;

    public Pill(string text, bool main)
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        this.main = main;
        Text = text;
        Font = Zen.UiFont(10f, true);
        Cursor = Cursors.Hand;
        Size = new Size(Zen.Px(main ? 112 : 96), Zen.Px(40));
    }

    protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hover = down = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e) { down = true; Invalidate(); base.OnMouseDown(e); }
    protected override void OnMouseUp(MouseEventArgs e) { down = false; Invalidate(); base.OnMouseUp(e); }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.Clear(Parent.BackColor);
        g.SmoothingMode = SmoothingMode.AntiAlias;
        Color fill = main ? (down ? Color.FromArgb(0xd0, 0xd0, 0xd0) : hover ? Color.White : Zen.Ink)
                          : (down ? Color.FromArgb(0x3a, 0x3a, 0x3a) : hover ? Color.FromArgb(0x33, 0x33, 0x33) : Zen.Surface);
        var box = new RectangleF(0.5f, 0.5f, Width - 2, Height - 2);
        using (var path = Zen.Rounded(box, box.Height / 2))
        using (var brush = new SolidBrush(fill))
        {
            g.FillPath(brush, path);
            if (!main) using (var line = new Pen(Zen.Line, Zen.Scale)) g.DrawPath(line, path);
        }
        if (Focused && ShowFocusCues)  // the keyboard's place, in the moon's colour
            using (var ring = Zen.Rounded(new RectangleF(1.5f, 1.5f, Width - 4, Height - 4), (Height - 4) / 2f))
            using (var pen = new Pen(Zen.Moon, 1.6f * Zen.Scale)) g.DrawPath(pen, ring);
        TextRenderer.DrawText(g, Text, Font, ClientRectangle, main ? Zen.Bg : Zen.Ink, fill,
                              TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine);
    }
}

// The window's ×, since it has no Windows frame
class CloseButton : Button
{
    bool hover;

    public CloseButton()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        Size = new Size(Zen.Px(36), Zen.Px(32));
        TabStop = false;
        Cursor = Cursors.Hand;
        AccessibleName = "Close";
    }

    protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hover = false; Invalidate(); base.OnMouseLeave(e); }

    protected override void OnPaint(PaintEventArgs e)
    {
        var g = e.Graphics;
        g.Clear(Parent.BackColor);
        g.SmoothingMode = SmoothingMode.AntiAlias;
        if (hover)
            using (var path = Zen.Rounded(new RectangleF(0, 0, Width - 1, Height - 1), Zen.Px(8)))
            using (var brush = new SolidBrush(Zen.Surface)) g.FillPath(brush, path);
        float c = Width / 2f, m = Height / 2f, r = Zen.Px(5);
        using (var pen = new Pen(hover ? Zen.Ink : Zen.Soft, 1.4f * Zen.Scale))
        {
            g.DrawLine(pen, c - r, m - r, c + r, m + r);
            g.DrawLine(pen, c + r, m - r, c - r, m + r);
        }
    }
}

// Without Edge: Zen opens in the default browser, and this small window keeps it running until it is closed
class RunningForm : Floating
{
    public RunningForm()
    {
        header = true;
        int pad = Zen.Px(28), width = Zen.Px(420);
        var close = new CloseButton { Location = new Point(width - Zen.Px(46), Zen.Px(10)) };
        close.Click += (s, e) => Close();
        var text = new Label { Text = "Zen is running at http://127.0.0.1:8000 in your browser. Closing this window stops it.",
                               ForeColor = Zen.Muted, AutoSize = false, Location = new Point(pad, Zen.Px(58)), Size = new Size(width - 2 * pad, Zen.Px(44)) };
        var stop = new Pill("Stop Zen", false);
        stop.Location = new Point(width - pad - stop.Width, Zen.Px(114));
        stop.Click += (s, e) => Close();
        ClientSize = new Size(width, stop.Bottom + Zen.Px(24));
        Controls.AddRange(new Control[] { close, text, stop });
    }
}

// Zen's own window: Windows' frame without its title bar, Zen's page inside (WebView2). The page is the title bar
// (web/index.html's .winbar): its top moves the window (app-region: drag), and it sends what its buttons and its top edge
// ask ("minimize", "maximize", "close", "size:top") and its colour ("theme:#1c1c1c"); this tells it back whether the
// window is maximized and active. Its own WebView2 profile (data\webview): no Edge account, no sync.
class ZenWindow : Form
{
    readonly WebView2 view = new TitleBarView();
    readonly string app, placeFile, folder;
    readonly TaskCompletionSource<bool> loaded = new TaskCompletionSource<bool>();
    FormWindowState shownAs, beforeFull;
    Rectangle normal;
    bool full;

    public ZenWindow(string app, string data)
    {
        this.app = app;
        placeFile = Path.Combine(data, "window.txt");
        folder = Path.Combine(data, "webview");
        Text = "Zen";
        Icon = Zen.AppIcon();
        AutoScaleMode = AutoScaleMode.None;
        BackColor = Zen.Bg;
        StartPosition = FormStartPosition.Manual;
        MinimumSize = new Size(Zen.Px(400), Zen.Px(480));
        Place();
        view.DefaultBackgroundColor = Zen.Bg;
        view.Dock = DockStyle.Fill;
        Controls.Add(view);
    }

    // The page loaded in the window, not shown yet; true once it has
    public Task<bool> Open()
    {
        view.CreateControl();  // the window and the view made, still hidden: WebView2 needs them
        if (view.Handle == IntPtr.Zero) return Task.FromResult(false);
        Start();
        return loaded.Task;
    }

    async void Start()
    {
        try
        {
            // Zen's entry sound plays as the window opens, before any click (a browser waits for one)
            var options = new CoreWebView2EnvironmentOptions("--autoplay-policy=no-user-gesture-required");
            await view.EnsureCoreWebView2Async(await CoreWebView2Environment.CreateAsync(null, folder, options));
            var web = view.CoreWebView2;
            web.Settings.IsNonClientRegionSupportEnabled = true;  // app-region: drag
            web.Settings.IsStatusBarEnabled = false;
            web.Settings.IsPasswordAutosaveEnabled = false;
            web.Settings.IsGeneralAutofillEnabled = false;
            web.DocumentTitleChanged += (s, e) => Text = web.DocumentTitle;
            web.WebMessageReceived += Heard;
            // Zen stays in its window; links to elsewhere open in the default browser
            web.NewWindowRequested += (s, e) => { e.Handled = true; Outside(e.Uri); };
            web.NavigationStarting += (s, e) => { if (!e.Uri.StartsWith(app)) { e.Cancel = true; Outside(e.Uri); } };
            web.ContainsFullScreenElementChanged += (s, e) => FullScreen(web.ContainsFullScreenElement);
            web.NavigationCompleted += (s, e) => { Tell(); loaded.TrySetResult(e.IsSuccess); };
            web.Navigate(app);
        }
        catch (Exception) { loaded.TrySetResult(false); }
    }

    static void Outside(string uri)
    {
        if (uri.StartsWith("https://") || uri.StartsWith("http://") || uri.StartsWith("mailto:"))
            try { Process.Start(uri); } catch (Exception) { }
    }

    void Heard(object sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        if (!e.Source.StartsWith(app)) return;
        string said;
        try { said = e.TryGetWebMessageAsString(); } catch (ArgumentException) { return; }  // JSON: not one of these
        // asked of Windows, as its own title bar's buttons do (WinForms' WindowState would restore the window a title
        // bar taller each time: it sizes it as if one were there)
        if (said == "minimize") ShowWindow(Handle, 6);  // SW_MINIMIZE
        else if (said == "maximize") ShowWindow(Handle, IsZoomed(Handle) ? 9 : 3);  // SW_RESTORE, SW_MAXIMIZE
        else if (said == "close") Close();
        else if (said.StartsWith("theme:")) Theme(said.Substring(6));
        else if (said.StartsWith("size:") && WindowState == FormWindowState.Normal)
        {
            // the page's top edge, held down: Windows takes the resize from here, as from its own frame
            int edge = said == "size:topleft" ? 13 : said == "size:topright" ? 14 : 12;  // HTTOPLEFT, HTTOPRIGHT, HTTOP
            var at = Cursor.Position;
            ReleaseCapture();
            SendMessage(Handle, 0xA1, (IntPtr)edge, (IntPtr)((at.Y << 16) | (at.X & 0xffff)));  // WM_NCLBUTTONDOWN
        }
    }

    // The window behind the page in the page's colour (seen while it resizes), and Windows' parts of it - the edge, the
    // shadow, the system menu - dark on a dark page
    void Theme(string colour)
    {
        try { BackColor = ColorTranslator.FromHtml(colour); } catch (Exception) { return; }
        view.DefaultBackgroundColor = BackColor;
        int dark = BackColor.GetBrightness() < 0.5f ? 1 : 0;
        DwmSetWindowAttribute(Handle, 20, ref dark, 4);  // DWMWA_USE_IMMERSIVE_DARK_MODE
    }

    // Maximized or not (the page's middle button: maximize or restore), active or not (its buttons fade when it isn't)
    void Tell()
    {
        if (view.CoreWebView2 == null) return;
        try
        {
            view.CoreWebView2.PostWebMessageAsString("maximized:" + (WindowState == FormWindowState.Maximized ? 1 : 0));
            view.CoreWebView2.PostWebMessageAsString("active:" + (ActiveForm == this ? 1 : 0));
        }
        catch (Exception) { }
    }

    protected override void OnResize(EventArgs e)
    {
        base.OnResize(e);
        if (WindowState != shownAs) { shownAs = WindowState; Tell(); }
    }

    protected override void OnActivated(EventArgs e) { base.OnActivated(e); Tell(); }
    protected override void OnDeactivate(EventArgs e) { base.OnDeactivate(e); Tell(); }

    // A page's full screen (a video, a game, the viewer's button): the whole screen, then back as it was
    void FullScreen(bool on)
    {
        if (on == full) return;
        full = on;
        if (on)
        {
            beforeFull = WindowState;
            if (IsZoomed(Handle)) ShowWindow(Handle, 9);
            normal = Bounds;
            FormBorderStyle = FormBorderStyle.None;
            Bounds = Screen.FromControl(this).Bounds;
        }
        else
        {
            FormBorderStyle = FormBorderStyle.Sizable;
            Bounds = normal;
            if (beforeFull == FormWindowState.Maximized) ShowWindow(Handle, 3);
        }
    }

    // Where it was last closed, when that is still on a screen; else 1320 x 880 in the middle of this one
    void Place()
    {
        try
        {
            var v = File.ReadAllText(placeFile).Split(' ');
            var r = new Rectangle(int.Parse(v[0]), int.Parse(v[1]), int.Parse(v[2]), int.Parse(v[3]));
            foreach (var screen in Screen.AllScreens)
            {
                if (!screen.WorkingArea.IntersectsWith(r)) continue;
                Bounds = r;
                if (v[4] == "max") WindowState = FormWindowState.Maximized;
                return;
            }
        }
        catch (Exception) { }
        var area = Screen.PrimaryScreen.WorkingArea;
        int w = Math.Min(Zen.Px(1320), area.Width - Zen.Px(64)), h = Math.Min(Zen.Px(880), area.Height - Zen.Px(48));
        Bounds = new Rectangle(area.Left + (area.Width - w) / 2, area.Top + (area.Height - h) / 2, w, h);
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (full) FullScreen(false);
        var r = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
        try { File.WriteAllText(placeFile, r.X + " " + r.Y + " " + r.Width + " " + r.Height + " " + (WindowState == FormWindowState.Maximized ? "max" : "normal")); }
        catch (Exception) { }
        base.OnFormClosing(e);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        int dark = 1;
        DwmSetWindowAttribute(Handle, 20, ref dark, 4);
        SetWindowPos(Handle, IntPtr.Zero, 0, 0, 0, 0, 0x27);  // SWP_FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER: the frame below
    }

    // Windows' frame without its title bar: the sizing edges (outside the window on Windows 10 and 11), the shadow, round
    // corners and snapping stay; the client area goes up to the top, where the page draws the bar
    protected override void WndProc(ref Message m)
    {
        if (m.Msg == 0x83 && m.WParam != IntPtr.Zero && !full)  // WM_NCCALCSIZE
        {
            var before = (Rect)Marshal.PtrToStructure(m.LParam, typeof(Rect));
            base.WndProc(ref m);
            var after = (Rect)Marshal.PtrToStructure(m.LParam, typeof(Rect));
            // maximized, a window reaches past the screen by its frame: the top comes down by as much as the sides do
            after.Top = before.Top + (IsZoomed(Handle) ? after.Left - before.Left : 0);
            Marshal.StructureToPtr(after, m.LParam, false);
            m.Result = IntPtr.Zero;
            return;
        }
        if (m.Msg == 0x47)  // WM_WINDOWPOSCHANGED
        {
            settling = true;
            try { base.WndProc(ref m); } finally { settling = false; }
            return;
        }
        base.WndProc(ref m);
    }

    // After a restore WinForms sizes the window again from the client size it remembers, as if Windows' title bar were
    // there (it would grow by one each time): Windows has already put it back right, so that is left as it is
    bool settling;
    protected override void SetBoundsCore(int x, int y, int width, int height, BoundsSpecified specified)
    {
        if (!settling) base.SetBoundsCore(x, y, width, height, specified);
    }

    [StructLayout(LayoutKind.Sequential)]
    struct Rect { public int Left, Top, Right, Bottom; }

    // WebView2 tells the window it sits in that the page's bar was pressed (title-bar messages: HTCAPTION) - here that is
    // this control, a child that cannot move: they go on to Zen's window, which moves, maximizes on a double click and
    // shows its menu on a right click, as from its own title bar
    class TitleBarView : WebView2
    {
        protected override void WndProc(ref Message m)
        {
            // WM_NCLBUTTONDOWN, WM_NCLBUTTONUP, WM_NCLBUTTONDBLCLK, WM_NCRBUTTONDOWN, WM_NCRBUTTONUP
            if (m.Msg >= 0xA1 && m.Msg <= 0xA5 && (int)m.WParam == 2 && TopLevelControl != null)
            {
                if (m.Msg == 0xA1) ReleaseCapture();
                m.Result = SendMessage(TopLevelControl.Handle, m.Msg, m.WParam, m.LParam);
                return;
            }
            base.WndProc(ref m);
        }
    }

    [DllImport("user32.dll")] static extern bool ReleaseCapture();
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool IsZoomed(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int cx, int cy, int flags);
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);
}
