using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.ServiceProcess;
using System.Text;
using System.Threading;

namespace Cortex.Brain
{
    internal sealed class ServiceSettings
    {
        public string PythonPath;
        public string ScriptPath;
        public string ConfigPath;
        public string LogPath;
        public int StopGraceSeconds = 45;

        public static ServiceSettings Parse(string[] arguments)
        {
            Dictionary<string, string> values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

            for (int i = 0; i < arguments.Length; i++)
            {
                string argument = arguments[i];
                if (!argument.StartsWith("--", StringComparison.Ordinal))
                {
                    throw new ArgumentException("Unexpected service argument: " + argument);
                }

                if (i + 1 >= arguments.Length)
                {
                    throw new ArgumentException("Missing value for service argument: " + argument);
                }

                values[argument.Substring(2)] = arguments[++i];
            }

            ServiceSettings settings = new ServiceSettings();
            settings.PythonPath = Required(values, "python");
            settings.ScriptPath = Required(values, "script");
            settings.ConfigPath = Required(values, "config");

            string logPath;
            if (values.TryGetValue("log", out logPath) && !String.IsNullOrWhiteSpace(logPath))
            {
                settings.LogPath = Path.GetFullPath(logPath);
            }
            else
            {
                string configDirectory = Path.GetDirectoryName(settings.ConfigPath);
                settings.LogPath = Path.Combine(configDirectory, "CortexGatewayService.log");
            }

            string graceValue;
            int graceSeconds;
            if (values.TryGetValue("stop-grace-seconds", out graceValue))
            {
                if (!Int32.TryParse(graceValue, out graceSeconds) || graceSeconds < 5 || graceSeconds > 120)
                {
                    throw new ArgumentException("--stop-grace-seconds must be between 5 and 120.");
                }

                settings.StopGraceSeconds = graceSeconds;
            }

            ValidateFile(settings.PythonPath, "Python interpreter");
            ValidateFile(settings.ScriptPath, "gateway script");
            ValidateFile(settings.ConfigPath, "gateway config");
            return settings;
        }

        private static string Required(Dictionary<string, string> values, string name)
        {
            string value;
            if (!values.TryGetValue(name, out value) || String.IsNullOrWhiteSpace(value))
            {
                throw new ArgumentException("Required service argument is missing: --" + name);
            }

            return Path.GetFullPath(value);
        }

        private static void ValidateFile(string path, string description)
        {
            if (!File.Exists(path))
            {
                throw new FileNotFoundException("The configured " + description + " was not found.", path);
            }
        }
    }

    internal sealed class CortexGatewayService : ServiceBase
    {
        private const string CortexServiceName = "CortexBrainGateway";
        private const string StopEventName = @"Global\CortexBrainGateway.Stop";
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private const int JobObjectExtendedLimitInformation = 9;

        private readonly ServiceSettings _settings;
        private readonly object _processLock = new object();
        private readonly object _logLock = new object();
        private readonly ManualResetEvent _shutdown = new ManualResetEvent(false);
        private EventWaitHandle _gatewayStopEvent;
        private Thread _supervisor;
        private Process _gatewayProcess;
        private StreamWriter _logWriter;
        private IntPtr _jobHandle = IntPtr.Zero;
        private int _stopStarted;

        public CortexGatewayService(ServiceSettings settings)
        {
            _settings = settings;
            ServiceName = CortexServiceName;
            CanStop = true;
            CanShutdown = true;
            CanPauseAndContinue = false;
            AutoLog = true;
        }

        protected override void OnStart(string[] args)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(_settings.LogPath));
            RotateLogIfNeeded(_settings.LogPath, 5L * 1024L * 1024L, 3);
            _logWriter = new StreamWriter(
                new FileStream(_settings.LogPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite),
                new UTF8Encoding(false));
            _logWriter.AutoFlush = true;

            bool createdNew;
            _gatewayStopEvent = new EventWaitHandle(false, EventResetMode.ManualReset, StopEventName, out createdNew);
            _gatewayStopEvent.Reset();
            _jobHandle = CreateKillOnCloseJob();

            Log("Service starting. Gateway worker will run in Windows Session 0.");
            _supervisor = new Thread(SuperviseGateway);
            _supervisor.IsBackground = true;
            _supervisor.Name = "Cortex Gateway Supervisor";
            _supervisor.Start();
        }

        protected override void OnStop()
        {
            StopWorker();
        }

        protected override void OnShutdown()
        {
            StopWorker();
            base.OnShutdown();
        }

        private void SuperviseGateway()
        {
            int restartDelaySeconds = 2;

            while (!_shutdown.WaitOne(0))
            {
                Process process = null;
                DateTime startedAt = DateTime.UtcNow;

                try
                {
                    lock (_processLock)
                    {
                        if (_shutdown.WaitOne(0))
                        {
                            break;
                        }

                        process = StartGatewayProcess();
                        _gatewayProcess = process;
                    }

                    Log("Gateway worker started with PID " + process.Id + ".");

                    while (!process.WaitForExit(1000))
                    {
                        if (_shutdown.WaitOne(0))
                        {
                            // OnStop signals the named event, waits for a graceful exit,
                            // and closes the job only if the grace period expires.
                            process.WaitForExit((_settings.StopGraceSeconds + 10) * 1000);
                            break;
                        }
                    }

                    if (!process.HasExited)
                    {
                        Log("Gateway worker was still active after the service stop wait.");
                        break;
                    }

                    process.WaitForExit();
                    int exitCode = process.ExitCode;
                    TimeSpan uptime = DateTime.UtcNow - startedAt;
                    Log("Gateway worker exited with code " + exitCode + " after " + FormatDuration(uptime) + ".");

                    if (uptime.TotalSeconds >= 120)
                    {
                        restartDelaySeconds = 2;
                    }
                    else
                    {
                        restartDelaySeconds = Math.Min(restartDelaySeconds * 2, 60);
                    }
                }
                catch (Exception exception)
                {
                    Log("Gateway supervisor error: " + exception);
                    restartDelaySeconds = Math.Min(restartDelaySeconds * 2, 60);
                }
                finally
                {
                    lock (_processLock)
                    {
                        if (Object.ReferenceEquals(_gatewayProcess, process))
                        {
                            _gatewayProcess = null;
                        }
                    }

                    if (process != null)
                    {
                        process.Dispose();
                    }
                }

                if (!_shutdown.WaitOne(0))
                {
                    Log("Restarting gateway worker in " + restartDelaySeconds + " seconds.");
                    _shutdown.WaitOne(TimeSpan.FromSeconds(restartDelaySeconds));
                }
            }

            Log("Gateway supervisor stopped.");
        }

        private Process StartGatewayProcess()
        {
            _gatewayStopEvent.Reset();

            ProcessStartInfo startInfo = new ProcessStartInfo();
            startInfo.FileName = _settings.PythonPath;
            startInfo.Arguments = QuoteArgument(_settings.ScriptPath)
                + " --config " + QuoteArgument(_settings.ConfigPath)
                + " --shutdown-event " + QuoteArgument(StopEventName);
            startInfo.WorkingDirectory = Path.GetDirectoryName(_settings.ScriptPath);
            startInfo.UseShellExecute = false;
            startInfo.CreateNoWindow = true;
            startInfo.WindowStyle = ProcessWindowStyle.Hidden;
            startInfo.RedirectStandardOutput = true;
            startInfo.RedirectStandardError = true;
            startInfo.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
            startInfo.EnvironmentVariables["CORTEX_SERVICE_MODE"] = "1";
            startInfo.EnvironmentVariables["CORTEX_SERVICE_STOP_EVENT"] = StopEventName;

            Process process = new Process();
            process.StartInfo = startInfo;
            process.EnableRaisingEvents = true;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
            {
                if (!String.IsNullOrEmpty(eventArgs.Data))
                {
                    Log("gateway: " + eventArgs.Data);
                }
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs eventArgs)
            {
                if (!String.IsNullOrEmpty(eventArgs.Data))
                {
                    Log("gateway stderr: " + eventArgs.Data);
                }
            };

            try
            {
                if (!process.Start())
                {
                    throw new InvalidOperationException("Process.Start returned false for the gateway worker.");
                }

                if (!AssignProcessToJobObject(_jobHandle, process.Handle))
                {
                    int error = Marshal.GetLastWin32Error();
                    try
                    {
                        process.Kill();
                    }
                    catch
                    {
                    }

                    throw new InvalidOperationException("Could not assign the gateway worker to its service job. Win32 error " + error + ".");
                }

                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                return process;
            }
            catch
            {
                process.Dispose();
                throw;
            }
        }

        private void StopWorker()
        {
            if (Interlocked.Exchange(ref _stopStarted, 1) != 0)
            {
                return;
            }

            try
            {
                RequestAdditionalTime((_settings.StopGraceSeconds + 15) * 1000);
            }
            catch
            {
            }

            Log("Service stop requested. Signalling the gateway worker.");
            Process process;
            lock (_processLock)
            {
                process = _gatewayProcess;
            }

            if (_gatewayStopEvent != null)
            {
                _gatewayStopEvent.Set();
            }

            _shutdown.Set();

            if (process != null)
            {
                try
                {
                    if (!process.HasExited && !process.WaitForExit(_settings.StopGraceSeconds * 1000))
                    {
                        Log("Gateway worker did not exit during the grace period; terminating its service job.");
                        CloseJobHandle();
                    }
                }
                catch (Exception exception)
                {
                    Log("Error while waiting for the gateway worker to stop: " + exception.Message);
                    CloseJobHandle();
                }
            }

            if (_supervisor != null && !_supervisor.Join(10000))
            {
                Log("Gateway supervisor did not stop within 10 seconds.");
            }

            CloseJobHandle();
            Log("Service stopped.");

            if (_gatewayStopEvent != null)
            {
                _gatewayStopEvent.Dispose();
                _gatewayStopEvent = null;
            }

            lock (_logLock)
            {
                if (_logWriter != null)
                {
                    _logWriter.Dispose();
                    _logWriter = null;
                }
            }
        }

        private void Log(string message)
        {
            lock (_logLock)
            {
                if (_logWriter != null)
                {
                    _logWriter.WriteLine(DateTimeOffset.Now.ToString("o") + " " + message);
                }
            }
        }

        private static string FormatDuration(TimeSpan duration)
        {
            return ((int)duration.TotalHours).ToString("00") + ":" + duration.Minutes.ToString("00") + ":" + duration.Seconds.ToString("00");
        }

        private static void RotateLogIfNeeded(string path, long maxBytes, int backups)
        {
            FileInfo current = new FileInfo(path);
            if (!current.Exists || current.Length < maxBytes)
            {
                return;
            }

            string oldest = path + "." + backups;
            if (File.Exists(oldest))
            {
                File.Delete(oldest);
            }

            for (int index = backups - 1; index >= 1; index--)
            {
                string source = path + "." + index;
                string destination = path + "." + (index + 1);
                if (File.Exists(source))
                {
                    File.Move(source, destination);
                }
            }

            File.Move(path, path + ".1");
        }

        private static string QuoteArgument(string value)
        {
            StringBuilder builder = new StringBuilder();
            builder.Append('"');
            int backslashes = 0;

            for (int i = 0; i < value.Length; i++)
            {
                char character = value[i];
                if (character == '\\')
                {
                    backslashes++;
                    continue;
                }

                if (character == '"')
                {
                    builder.Append('\\', backslashes * 2 + 1);
                    builder.Append('"');
                    backslashes = 0;
                    continue;
                }

                builder.Append('\\', backslashes);
                backslashes = 0;
                builder.Append(character);
            }

            builder.Append('\\', backslashes * 2);
            builder.Append('"');
            return builder.ToString();
        }

        private static IntPtr CreateKillOnCloseJob()
        {
            IntPtr job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero)
            {
                throw new InvalidOperationException("Could not create the gateway service job. Win32 error " + Marshal.GetLastWin32Error() + ".");
            }

            JOBOBJECT_EXTENDED_LIMIT_INFORMATION information = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            information.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr buffer = Marshal.AllocHGlobal(size);

            try
            {
                Marshal.StructureToPtr(information, buffer, false);
                if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, buffer, (uint)size))
                {
                    int error = Marshal.GetLastWin32Error();
                    CloseHandle(job);
                    throw new InvalidOperationException("Could not configure the gateway service job. Win32 error " + error + ".");
                }
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }

            return job;
        }

        private void CloseJobHandle()
        {
            IntPtr handle = Interlocked.Exchange(ref _jobHandle, IntPtr.Zero);
            if (handle != IntPtr.Zero)
            {
                CloseHandle(handle);
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public IntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(IntPtr job, int informationClass, IntPtr information, uint informationLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);

        public static void Main(string[] arguments)
        {
            ServiceSettings settings = ServiceSettings.Parse(arguments);
            ServiceBase.Run(new CortexGatewayService(settings));
        }
    }
}
