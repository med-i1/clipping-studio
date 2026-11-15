@echo off
echo Building Clipper Studio...

pyinstaller --onefile --windowed --name "Clipping Studio" --add-data "clipper/lang;lang" --add-data "prompts;prompts" --add-data "clipper/viral/web;web" --icon icon.ico cli.py

echo Done.
pause