// Zen.exe: starts Zen harness on this computer and opens it in a window of its own.
//
// The folder it sits in (desktop/build.py makes it):
//     Zen.exe          this launcher
//     settings.env     the model's address and password, and any other setting (NAME=value lines)
//     runtime\         Python with Zen's libraries (the code sandbox runs scripts with this Python too)
//     zen\             app\, web\ and kaggle\bridge.py
//     tools\           node and Playwright MCP: the browser Zen reads blocked pages with and tries its games in
//     data\            projects, chats and files - everything that is yours; logs\ holds the servers' output
//
// One click: the bridge to the model on Kaggle (when settings.env names one), then the app, then the window (Edge's
// app mode, its own profile). Closing the window stops both. A second click while Zen runs opens another window.
// Built by desktop/build.py with the C# compiler every Windows has (.NET Framework 4): C# 5, no packages.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

static class Zen
{
    const string App = "http://127.0.0.1:8000/";
    const string Model = "http://127.0.0.1:8080/health";  // the bridge, or a llama-server on this computer
    static readonly string Root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
    static readonly string SettingsFile = Path.Combine(Root, "settings.env");
    static readonly string Data = Path.Combine(Root, "data");
    static readonly string Logs = Path.Combine(Data, "logs");
    static readonly List<Process> started = new List<Process>();

    [STAThread]
    static int Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        if (Answers(App + "assistant/status", 1500))  // already running: one more window
        {
            OpenWindow(false);
            return 0;
        }
        Directory.CreateDirectory(Logs);
        try
        {
            var settings = ReadSettings();
            if (!Connect(settings)) return 1;
            using (var splash = new Splash("Starting Zen…"))
            {
                splash.Show();
                var app = Start(Python(), "-m uvicorn app.main:app --host 127.0.0.1 --port 8000", "zen.log", settings);
                if (!WaitFor(App + "assistant/status", 120, app, splash))
                {
                    splash.Close();
                    Fail("Zen didn't start.", Path.Combine(Logs, "zen.log"));
                    return 1;
                }
            }
            OpenWindow(true);
            return 0;
        }
        finally
        {
            StopAll();
        }
    }

    // The model: the Kaggle bridge when settings.env names an address - asked for when there is none, or when it no
    // longer answers (the notebook's tunnel gets a new address each time it starts) - or a llama-server already here.
    static bool Connect(Dictionary<string, string> settings)
    {
        string why = null;
        while (true)
        {
            string url;
            if (!settings.TryGetValue("KAGGLE_URL", out url) || url.Length == 0)
            {
                if (why == null && Answers(Model, 1500)) return true;  // a llama-server on this computer
                why = why ?? "Zen thinks with Qwen 3.6, which runs free on a Kaggle notebook (zen\\kaggle has it). " +
                              "Start the notebook, then paste the address and password its last cell prints.";
            }
            else
            {
                Process bridge = null;
                using (var splash = new Splash("Reaching the model…"))
                {
                    splash.Show();
                    bridge = Start(Python(), "\"" + Path.Combine(Root, "zen", "kaggle", "bridge.py") + "\"", "bridge.log", settings);
                    if (WaitFor(Model, 25, bridge, splash)) return true;
                }
                Stop(bridge);
                why = "The model didn't answer at that address. Is the notebook still running? It prints a new " +
                      "address and password each time it starts.";
            }
            using (var ask = new ConnectForm(why, settings))
            {
                if (ask.ShowDialog() != DialogResult.OK) return false;
                settings["KAGGLE_URL"] = ask.Address;
                settings["KAGGLE_PASSWORD"] = ask.Password;
                WriteSettings(settings);
            }
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
        env["BIZ_EMBED_CACHE_DIR"] = Path.Combine(Data, "models");
        if (!env.ContainsKey("BIZ_LLM_PROVIDER") || string.IsNullOrEmpty(env["BIZ_LLM_PROVIDER"])) env["BIZ_LLM_PROVIDER"] = "llamacpp";
        string mcp = Path.Combine(Root, "tools", "playwright-mcp");
        if (Directory.Exists(mcp) && string.IsNullOrEmpty(env["ASSISTANT_PLAYWRIGHT_MCP"])) env["ASSISTANT_PLAYWRIGHT_MCP"] = mcp;
        env["PATH"] = Path.Combine(Root, "tools", "node") + ";" + Path.Combine(Root, "runtime") + ";" + env["PATH"];
        env["PYTHONUTF8"] = "1";
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

    // Until url answers, the process exits or the time is up - the splash kept painting meanwhile
    static bool WaitFor(string url, int seconds, Process proc, Form splash)
    {
        var end = DateTime.Now.AddSeconds(seconds);
        while (DateTime.Now < end)
        {
            if (Answers(url, 1000)) return true;
            if (proc.HasExited) return false;
            for (int i = 0; i < 5; i++) { Application.DoEvents(); Thread.Sleep(100); }
        }
        return false;
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

    // Edge's app mode, a profile of its own (a window, not a tab among the user's): it stays open until the window is
    // closed. Without Edge, the default browser, and a small window that stops Zen when closed.
    static void OpenWindow(bool wait)
    {
        string edge = FindEdge();
        if (edge != null)
        {
            var profile = Path.Combine(Data, "window");
            var args = "--app=" + App + " --user-data-dir=\"" + profile + "\" --no-first-run --no-default-browser-check " +
                       "--disable-features=msEdgeSidebarV2 --window-size=1320,880";
            var window = Process.Start(new ProcessStartInfo(edge, args) { UseShellExecute = false });
            if (wait && window != null) window.WaitForExit();
            return;
        }
        Process.Start(App);
        if (wait) Application.Run(new RunningForm());
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

    // The address and password replaced in place; every other line - comments, other settings - kept as it was
    static void WriteSettings(Dictionary<string, string> settings)
    {
        var lines = File.Exists(SettingsFile) ? new List<string>(File.ReadAllLines(SettingsFile, Encoding.UTF8)) : new List<string>();
        foreach (var name in new[] { "KAGGLE_URL", "KAGGLE_PASSWORD" })
        {
            int at = lines.FindIndex(l => l.TrimStart().StartsWith(name + "="));
            var line = name + "=" + settings[name];
            if (at >= 0) lines[at] = line; else lines.Add(line);
        }
        File.WriteAllLines(SettingsFile, lines.ToArray(), new UTF8Encoding(false));
    }

    static void Fail(string what, string log)
    {
        string tail = "";
        try
        {
            var lines = File.ReadAllLines(log);
            tail = string.Join("\n", lines, Math.Max(0, lines.Length - 12), Math.Min(12, lines.Length));
        }
        catch (Exception) { }
        MessageBox.Show(what + "\n\nThe last lines of " + log + ":\n\n" + tail, "Zen harness", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    // Zen's own colours (web/app.css, dark): the page, its text, the muted text, the line between things
    internal static readonly Color Bg = Color.FromArgb(0x21, 0x21, 0x21);
    internal static readonly Color Ink = Color.FromArgb(0xec, 0xec, 0xec);
    internal static readonly Color Muted = Color.FromArgb(0x9b, 0x9b, 0x9b);
    internal static readonly Color Field = Color.FromArgb(0x30, 0x30, 0x30);
    internal static readonly Color Moon = Color.FromArgb(0x8e, 0x7d, 0xf5);

    internal static Font UiFont(float size, FontStyle style = FontStyle.Regular) { return new Font("Segoe UI", size, style); }

    internal static Icon AppIcon()
    {
        try { return Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch (Exception) { return null; }
    }

    [System.Runtime.InteropServices.DllImport("dwmapi.dll")]
    static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

    // A dark title bar over a dark window (Windows 10 1809 and later; elsewhere nothing changes)
    internal static void DarkTitle(Form form)
    {
        int on = 1;
        try { if (DwmSetWindowAttribute(form.Handle, 20, ref on, 4) != 0) DwmSetWindowAttribute(form.Handle, 19, ref on, 4); }
        catch (Exception) { }
    }
}

// "Starting Zen…": the crescent and a line, while the servers come up
class Splash : Form
{
    public Splash(string text)
    {
        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.CenterScreen;
        Size = new Size(360, 150);
        BackColor = Zen.Bg;
        ShowInTaskbar = true;
        Text = "Zen harness";
        Icon = Zen.AppIcon();
        var label = new Label { Text = text, ForeColor = Zen.Ink, Font = Zen.UiFont(11f), AutoSize = false,
                                TextAlign = ContentAlignment.MiddleCenter, Dock = DockStyle.Bottom, Height = 48 };
        Controls.Add(label);
        Paint += (s, e) =>
        {
            e.Graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            var r = new Rectangle(Width / 2 - 22, 30, 44, 44);
            using (var moon = new SolidBrush(Zen.Moon)) e.Graphics.FillEllipse(moon, r);
            using (var cut = new SolidBrush(Zen.Bg)) e.Graphics.FillEllipse(cut, new Rectangle(r.X + 14, r.Y - 9, 42, 42));
        };
    }
}

// The model's address and password, from the Kaggle notebook's last cell
class ConnectForm : Form
{
    readonly TextBox address = new TextBox(), password = new TextBox();
    public string Address { get { return address.Text.Trim().TrimEnd('/'); } }
    public string Password { get { return password.Text.Trim(); } }

    public ConnectForm(string why, Dictionary<string, string> settings)
    {
        Text = "Zen harness - connect the model";
        Icon = Zen.AppIcon();
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = MinimizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Zen.Bg;
        ForeColor = Zen.Ink;
        Font = Zen.UiFont(10f);
        ClientSize = new Size(520, 300);
        Padding = new Padding(24);

        var title = new Label { Text = "Connect Zen to its model", Font = Zen.UiFont(15f, FontStyle.Bold), AutoSize = true, Location = new Point(24, 20) };
        var lede = new Label { Text = why, ForeColor = Zen.Muted, AutoSize = false, Location = new Point(24, 58), Size = new Size(472, 62) };
        var a = new Label { Text = "Address", AutoSize = true, Location = new Point(24, 128) };
        var p = new Label { Text = "Password", AutoSize = true, Location = new Point(24, 184) };
        foreach (var box in new[] { address, password })
        {
            box.BackColor = Zen.Field;
            box.ForeColor = Zen.Ink;
            box.BorderStyle = BorderStyle.FixedSingle;
            box.Width = 472;
        }
        address.Location = new Point(24, 150);
        password.Location = new Point(24, 206);
        password.UseSystemPasswordChar = true;
        string old;
        if (settings.TryGetValue("KAGGLE_URL", out old)) address.Text = old;

        var connect = new Button { Text = "Connect", DialogResult = DialogResult.OK, Location = new Point(316, 252), Size = new Size(88, 30),
                                   BackColor = Zen.Ink, ForeColor = Zen.Bg, FlatStyle = FlatStyle.Flat };
        var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Location = new Point(408, 252), Size = new Size(88, 30),
                                  BackColor = Zen.Field, ForeColor = Zen.Ink, FlatStyle = FlatStyle.Flat };
        connect.FlatAppearance.BorderSize = 0;
        cancel.FlatAppearance.BorderColor = Color.FromArgb(0x44, 0x44, 0x44);
        AcceptButton = connect;
        CancelButton = cancel;
        connect.Click += (s, e) =>
        {
            if (!Address.StartsWith("https://") && !Address.StartsWith("http://") || Password.Length == 0)
            {
                MessageBox.Show(this, "Paste both lines the notebook printed: an address starting with https:// and its password.", Text);
                DialogResult = DialogResult.None;
            }
        };
        Controls.AddRange(new Control[] { title, lede, a, address, p, password, connect, cancel });
    }

    protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); Zen.DarkTitle(this); }
}

// Without Edge: Zen opens in the default browser, and this small window keeps it running until it is closed
class RunningForm : Form
{
    public RunningForm()
    {
        Text = "Zen harness";
        Icon = Zen.AppIcon();
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Zen.Bg;
        ForeColor = Zen.Ink;
        Font = Zen.UiFont(10f);
        ClientSize = new Size(380, 130);
        var text = new Label { Text = "Zen is running at http://127.0.0.1:8000 in your browser.\nClose this window to stop it.",
                               AutoSize = false, Location = new Point(20, 20), Size = new Size(340, 50) };
        var stop = new Button { Text = "Stop Zen", Location = new Point(272, 84), Size = new Size(88, 30), BackColor = Zen.Field, FlatStyle = FlatStyle.Flat };
        stop.Click += (s, e) => Close();
        Controls.AddRange(new Control[] { text, stop });
    }

    protected override void OnHandleCreated(EventArgs e) { base.OnHandleCreated(e); Zen.DarkTitle(this); }
}
