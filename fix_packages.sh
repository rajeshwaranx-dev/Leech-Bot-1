#!/bin/bash
echo "Fixing Python 3.12 compatibility..."
pip install --break-system-packages -r requirements.txt
pip install --break-system-packages "tenacity==8.2.3"
sed -i 's/from re import sre_parse, U/import sre_parse; from re import U/' /usr/local/lib/python3.12/dist-packages/lk21/thirdparty/exrex.py
echo "Done! Now run: bash start.sh"
