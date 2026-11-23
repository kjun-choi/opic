@echo off
chcp 65001 > nul
set "PYTHON=%LOCALAPPDATA%\Python\bin\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo ==============================================
echo  OPIc 스크립트(scripts/) -> 영어 MP3(mp3/) 변환기
echo ==============================================
echo  1) 기본 청취용 (../mp3 폴더, 자연스러운 속도)
echo  2) 쉐도잉 연습용 (../mp3_shadowing 폴더, 문장 사이 2.5초 공백, 0.9배속)
echo  3) 문장 2번 반복 쉐도잉 (../mp3_shadowing 폴더)
echo ==============================================
set /p CHOICE="번호를 선택하세요 (기본 1): "

if "%CHOICE%"=="2" (
    "%PYTHON%" "%~dp0make_mp3.py" --mode shadowing --pause 2.5 --rate=-10%%
) else if "%CHOICE%"=="3" (
    "%PYTHON%" "%~dp0make_mp3.py" --mode shadowing --repeat 2 --pause 2.0 --rate=-5%%
) else (
    "%PYTHON%" "%~dp0make_mp3.py"
)

echo.
pause
