@echo off
cd /d "%~dp0"
echo 웹앱을 시작합니다. 잠시 후 브라우저가 자동으로 열립니다...
echo (이 검은 창을 닫으면 웹앱도 꺼집니다. 다 쓰실 때까지 이 창은 그대로 두세요)
python -m streamlit run app.py
pause
