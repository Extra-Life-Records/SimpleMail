@echo off
rem Native ARM64 packages are now built and published by .github/workflows/build.yml.
rem This former manual uploader could create incomplete releases; use the tag workflow.
echo ARM64 desktop and console agent builds are automatic.
echo After merging passing checks, tag the version and push that tag to origin.
echo The release is published only after both x64 and ARM64 package checks pass.
exit /b 0
