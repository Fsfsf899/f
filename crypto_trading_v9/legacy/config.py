"""
إعدادات آمنة للـ API
Configuration for Binance API

⚠️ تحذير أمان:
   - لا تنشر API keys على GitHub
   - استخدم .env file بدلاً من hardcoding
   - أضف قيود على الـ API key (Read-only للبداية)
"""

import os
from dotenv import load_dotenv

# تحميل من .env file
load_dotenv()

# ==================== إعدادات Binance ====================

# استخدم متغيرات البيئة (آمن جداً)
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET', '')

# الشبكة (testnet أولاً!)
USE_TESTNET = os.getenv('USE_TESTNET', 'True') == 'True'

if USE_TESTNET:
    BINANCE_API_URL = "https://testnet.binance.vision/api"
else:
    BINANCE_API_URL = "https://api.binance.com/api"


# ==================== إعدادات التداول ====================

TRADING_CONFIG = {
    # الأزواج المراقبة
    'pairs': [
        'BTCUSDT',
        'ETHUSDT',
        'ADAUSDT',
        'BNBUSDT',
        'BNBBTC',
        'ETHBTC'
    ],
    
    # إدارة رأس المال
    'max_open_positions': 3,          # أقصى صفقات مفتوحة
    'risk_per_trade': 2.0,            # % من الرصيد لكل صفقة
    'daily_loss_limit': 5.0,          # % من الرصيد يومياً
    
    # Trailing Stop Loss
    'trailing_stop_percent': 2.0,     # 2% من الربح
    'use_trailing_stop': True,
    
    # إعدادات الدخول
    'min_signal_strength': 3,         # حد أدنى 3 نجوم
    'min_conditions': 4,              # حد أدنى 4 شروط
    
    # حد التداول
    'min_order_value': 10,            # أدنى قيمة صفقة ($)
    'max_order_value': 1000,          # أقصى قيمة صفقة ($)
}


# ==================== إعدادات المؤشرات ====================

INDICATORS_CONFIG = {
    # EMA
    'ema_fast_period': 20,
    'ema_slow_period': 50,
    
    # RSI
    'rsi_period': 14,
    'rsi_oversold': 30,
    'rsi_overbought': 70,
    
    # MACD
    'macd_fast': 12,
    'macd_slow': 26,
    'macd_signal': 9,
    
    # ATR
    'atr_period': 14,
    'atr_multiplier': 2.5,
    
    # Bollinger Bands
    'bb_period': 20,
    'bb_std_dev': 2.0,
    
    # Stochastic
    'stoch_period': 14,
    'stoch_smooth_k': 3,
    'stoch_smooth_d': 3,
    
    # ADX
    'adx_period': 14,
    'adx_threshold': 20,
}


# ==================== إعدادات التنبيهات ====================

ALERTS_CONFIG = {
    # Email
    'email_alerts': False,
    'email_address': os.getenv('EMAIL_ADDRESS', ''),
    'email_password': os.getenv('EMAIL_PASSWORD', ''),
    
    # Telegram
    'telegram_alerts': False,
    'telegram_token': os.getenv('TELEGRAM_TOKEN', ''),
    'telegram_chat_id': os.getenv('TELEGRAM_CHAT_ID', ''),
    
    # Discord
    'discord_alerts': False,
    'discord_webhook': os.getenv('DISCORD_WEBHOOK', ''),
    
    # Browser Notification
    'browser_alerts': True,
}


# ==================== إعدادات السجل ====================

LOGGING_CONFIG = {
    'log_file': 'trading_log.json',
    'backup_file': 'trading_log_backup.json',
    'max_log_size': 10000,  # عدد الصفقات قبل نسخ احتياطي
    'log_level': 'INFO',     # DEBUG, INFO, WARNING, ERROR
}


# ==================== دالة التحقق ====================

def validate_config():
    """التحقق من صحة الإعدادات"""
    
    errors = []
    
    # تحقق من API keys
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        errors.append("⚠️  API keys غير مكتملة - استخدم .env file")
    
    # تحقق من إعدادات التداول
    if TRADING_CONFIG['risk_per_trade'] > 5:
        errors.append("⚠️  risk_per_trade مرتفع جداً (استخدم أقل من 5%)")
    
    if TRADING_CONFIG['daily_loss_limit'] < 2:
        errors.append("⚠️  daily_loss_limit منخفض جداً")
    
    if not USE_TESTNET:
        errors.append("🔴 تحذير: استخدم TESTNET أولاً!")
    
    return errors


def print_config_status():
    """طباعة حالة الإعدادات"""
    
    print("""
╔════════════════════════════════════════════════════════════════════╗
║                   ⚙️ حالة الإعدادات                              ║
╚════════════════════════════════════════════════════════════════════╝
    """)
    
    # API
    if BINANCE_API_KEY:
        print("✅ API Key مكتمل")
    else:
        print("❌ API Key ناقص")
    
    # Network
    if USE_TESTNET:
        print("✅ وضع الاختبار (Testnet) - آمن جداً")
    else:
        print("🔴 وضع حقيقي - احذر!")
    
    # Risk
    print(f"📊 risk_per_trade: {TRADING_CONFIG['risk_per_trade']}%")
    print(f"📊 daily_loss_limit: {TRADING_CONFIG['daily_loss_limit']}%")
    print(f"📊 max_open_positions: {TRADING_CONFIG['max_open_positions']}")
    
    # Trailing Stop
    if TRADING_CONFIG['use_trailing_stop']:
        print(f"✅ Trailing Stop مفعل ({TRADING_CONFIG['trailing_stop_percent']}%)")
    else:
        print("❌ Trailing Stop معطل")
    
    # Pairs
    print(f"📊 الأزواج المراقبة: {', '.join(TRADING_CONFIG['pairs'])}")
    
    # Alerts
    enabled_alerts = [k for k, v in ALERTS_CONFIG.items() if k.endswith('_alerts') and v]
    if enabled_alerts:
        print(f"🔔 التنبيهات المفعلة: {', '.join(enabled_alerts)}")
    else:
        print("🔕 التنبيهات معطلة")
    
    # Validation
    errors = validate_config()
    if errors:
        print("\n⚠️  تحذيرات:")
        for error in errors:
            print(f"  {error}")
    else:
        print("\n✅ جميع الإعدادات صحيحة!")


if __name__ == "__main__":
    print_config_status()
