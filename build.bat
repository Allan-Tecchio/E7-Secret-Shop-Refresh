@echo off
rem Builds E7ShopRefresh.exe (asks for administrator when opened)
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --uac-admin --name E7ShopRefresh --add-data "icons;icons" --distpath . e7_shop_gui.pyw
