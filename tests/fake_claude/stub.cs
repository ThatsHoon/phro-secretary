// claude.exe stand-in compiled by tests/e2e_desktop.mjs: forwards arguments to claude.py intact.
using System; using System.Diagnostics; using System.Text;
class Stub {
  static int Main(string[] args) {
    var psi = new ProcessStartInfo(Environment.GetEnvironmentVariable("FAKE_CLAUDE_PY"),
                                   "\"" + Environment.GetEnvironmentVariable("FAKE_CLAUDE_SCRIPT") + "\"");
    var encoded = new StringBuilder();
    foreach (var a in args) { if (encoded.Length > 0) encoded.Append('\n'); encoded.Append(Convert.ToBase64String(Encoding.UTF8.GetBytes(a))); }
    psi.EnvironmentVariables["FAKE_CLAUDE_ARGS"] = encoded.ToString();
    psi.UseShellExecute = false; psi.RedirectStandardOutput = true; psi.StandardOutputEncoding = Encoding.UTF8;
    var p = Process.Start(psi);
    var output = Encoding.UTF8.GetBytes(p.StandardOutput.ReadToEnd());
    p.WaitForExit();
    Console.OpenStandardOutput().Write(output, 0, output.Length);
    return p.ExitCode;
  }
}
