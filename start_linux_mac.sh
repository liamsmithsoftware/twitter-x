#!/usr/bin/env bash
# اجرای برنامه روی لینوکس و مک
# استفاده:  bash start_linux_mac.sh   یا   ./start_linux_mac.sh
set -e

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "خطا: پایتون 3 یافت نشد. لطفاً Python 3.10 یا بالاتر را نصب کنید."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "→ ساخت محیط مجازی پایتون ..."
    "$PYTHON_BIN" -m venv .venv
fi

echo "→ نصب / بررسی وابستگی‌ها ..."
./.venv/bin/python -m pip install --upgrade pip >/dev/null

# نصب فقط از فایل‌های wheel آماده (بدون نیاز به کامپایلر C)
if ! ./.venv/bin/python -m pip install --prefer-binary -r requirements.txt; then
    echo "هشدار: نصب عادی ناموفق بود؛ تلاش دوباره با گزینه‌های بیشتر ..."
    ./.venv/bin/python -m pip install --prefer-binary --retries 5 --timeout 60 -r requirements.txt
fi

echo "→ بررسی مرورگر Chromium برای دریافت از وب عمومی ..."
if ! ./.venv/bin/python -m playwright install --only-shell chromium; then
    echo "مرورگر دریافت نصب نشد. آرشیو و دادهٔ نمونه در دسترس است؛ برای دریافت واقعی دستور بالا را دوباره اجرا کنید."
fi

echo "→ اجرای برنامه ..."
exec ./.venv/bin/python run.py "$@"
