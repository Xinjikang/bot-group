' Hidden launcher; derive the repository root from this file.
' Use the same environment and health check as the interactive launcher.
Option Explicit
Dim WshShell, Fso, Root, ExitCode
Set WshShell = CreateObject("WScript.Shell")
Set Fso = CreateObject("Scripting.FileSystemObject")
Root = Fso.GetParentFolderName(Fso.GetParentFolderName(WScript.ScriptFullName))
WshShell.CurrentDirectory = Root
ExitCode = WshShell.Run("""D:\Program Files\Git\bin\bash.exe"" """ & Root & "\start.sh""", 0, True)
WScript.Quit ExitCode
