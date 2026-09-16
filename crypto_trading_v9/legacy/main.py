"""
البرنامج الرئيسي - نقطة الدخول الموحدة
Main Entry Point - Crypto Trading Tool

يربط جميع المراحل:
1. جلب البيانات
2. حساب المؤشرات
3. إشارات الدخول/الخروج
4. الباكتستنق
5. المراقبة الحية
"""

import sys
import json
from datetime import datetime


def print_menu():
    """عرض القائمة الرئيسية"""
    print("\n" + "="*70)
    print("🚀 أداة التداول الكريبتوجرافية - القائمة الرئيسية")
    print("="*70)
    print()
    print("ماذا تريد أن تفعل؟")
    print()
    print("1️⃣  الباكتستنق المحسّن (موصى به)")
    print("   → اختبر الإشارات على بيانات تاريخية 30 يوم")
    print()
    print("2️⃣  الباكتستنق البسيط")
    print("   → نسخة مبسطة بدون مكتبات إضافية")
    print()
    print("3️⃣  المراقبة الحية (مرحلة 5)")
    print("   → مراقبة الأسعار وإصدار الإشارات بشكل حي")
    print()
    print("4️⃣  اختبار جلب البيانات")
    print("   → جرّب جلب البيانات من Binance (أو تجريبية)")
    print()
    print("5️⃣  اختبار المؤشرات")
    print("   → احسب EMA, RSI, MACD, ATR على بيانات تجريبية")
    print()
    print("6️⃣  دليل الاستخدام")
    print("   → اقرأ تعليمات مفصلة")
    print()
    print("0️⃣  خروج")
    print()
    print("-" * 70)


def option_1_optimized_backtest():
    """تشغيل الباكتستنق المحسّن"""
    print("\n⏳ جاري تشغيل الباكتستنق المحسّن...")
    print("(يأخذ حوالي 10-30 ثانية)\n")
    
    try:
        import subprocess
        result = subprocess.run([sys.executable, 'backtest_optimized.py'], 
                              capture_output=False)
        return result.returncode == 0
    except Exception as e:
        print(f"❌ خطأ: {e}")
        return False


def option_2_simple_backtest():
    """تشغيل الباكتستنق البسيط"""
    print("\n⏳ جاري تشغيل الباكتستنق البسيط...\n")
    
    try:
        import subprocess
        result = subprocess.run([sys.executable, 'backtest_standalone.py'], 
                              capture_output=False)
        return result.returncode == 0
    except Exception as e:
        print(f"❌ خطأ: {e}")
        return False


def option_3_live_monitor():
    """تشغيل المراقبة الحية"""
    print("\n" + "="*70)
    print("⚠️  نظام المراقبة الحية")
    print("="*70)
    print("\nتحذيرات مهمة:")
    print("1. هذا وضع اختبار بـ بيانات وهمية")
    print("2. للبيانات الحقيقية: ثبّت ccxt وأضف مفتاح API من Binance")
    print("3. للتنبيهات عبر Telegram: أحصل على توكن وأضفه")
    print()
    
    confirm = input("هل تريد المتابعة؟ (yes/no): ").lower()
    
    if confirm != 'yes':
        print("❌ تم الإلغاء")
        return False
    
    print("\n⏳ جاري تشغيل المراقبة الحية (2 دقيقة اختبار)...\n")
    
    try:
        import subprocess
        result = subprocess.run([sys.executable, 'live_monitor.py'], 
                              capture_output=False)
        return result.returncode == 0
    except Exception as e:
        print(f"❌ خطأ: {e}")
        return False


def option_4_test_data_fetcher():
    """اختبار جلب البيانات"""
    print("\n⏳ اختبار جلب البيانات...\n")
    
    print("محاولة جلب من Binance...")
    try:
        import subprocess
        result = subprocess.run([sys.executable, 'binance_fetcher.py'], 
                              capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0:
            print(result.stdout)
            return True
        else:
            print("⚠️  فشل الاتصال بـ Binance (قد تكون محظور أو بدون اتصال)")
            print("\nسأستخدم البيانات التجريبية بدلاً منها...")
            
            # بيانات تجريبية
            print("\n✅ بيانات تجريبية متاحة")
            return True
    
    except Exception as e:
        print(f"⚠️  {e}")
        print("استخدام بيانات تجريبية...")
        return True


def option_5_test_indicators():
    """اختبار المؤشرات"""
    print("\n⏳ اختبار حساب المؤشرات...\n")
    
    try:
        import subprocess
        result = subprocess.run([sys.executable, 'indicators.py'], 
                              capture_output=False)
        return result.returncode == 0
    except Exception as e:
        print(f"❌ خطأ: {e}")
        return False


def option_6_guide():
    """عرض الدليل"""
    print("\n" + "="*70)
    print("📖 دليل الاستخدام")
    print("="*70 + "\n")
    
    guide = """
════════════════════════════════════════════════════════════════════════
                      🚀 البدء السريع
════════════════════════════════════════════════════════════════════════

1. الخطوة الأولى: الباكتستنق (اختبار الإشارات على بيانات قديمة)
   ┌─────────────────────────────────────────────────────────────┐
   │ اختر "1️⃣  الباكتستنق المحسّن"                            │
   │ سيظهر تقرير بـ:                                            │
   │ • عدد الصفقات                                              │
   │ • نسبة النجاح                                              │
   │ • الربح/الخسارة                                            │
   │ • Profit Factor                                            │
   └─────────────────────────────────────────────────────────────┘

2. ماذا تعني النتائج؟
   
   ✅ نتائج جيدة (استمر):
   • Win Rate > 40%
   • Profit Factor > 1.5
   • net_profit > 0

   ⚠️ نتائج سيئة (غيّر المعاملات):
   • Win Rate < 30%
   • Profit Factor < 1.0
   • خسائر متتالية

3. كيف تحسّن؟
   
   عدّل هذه القيم في backtest_optimized.py:
   
   a) فترات EMA:
      ema_fast_period = 20   (جرّب 9, 12, 20, 50)
      ema_slow_period = 50   (جرّب 21, 26, 50, 200)
   
   b) ATR Multiplier (حجم الوقف/الهدف):
      atr_stop = 2.5         (جرّب 1.5, 2.0, 2.5, 3.0)
      atr_target = 2.5       (جرّب 2.0, 2.5, 3.0)
   
   c) RSI limits (حساسية الإشارات):
      في signal_engine.py:
      if rsi[idx] > 80      (جرّب 70, 75, 80, 90)
      if rsi[idx] < 20      (جرّب 10, 20, 30, 40)

4. بعد الباكتستنق: التداول الحي
   
   ⚠️ تحذيرات:
   • ابدأ برأس مال صغير (1% من كل رأس المال)
   • اختبر مع بيانات حقيقية (Binance API)
   • راقب 10-20 صفقة قبل ما تزيد المخاطر
   • تجنب التداول في ساعات الـ high volatility

════════════════════════════════════════════════════════════════════════
                   ملفات المشروع الكاملة
════════════════════════════════════════════════════════════════════════

1. data_fetcher.py        → جلب البيانات
2. indicators.py          → حساب المؤشرات
3. signal_engine.py       → قواعس الدخول/الخروج
4. backtesting.py         → باكتستنق شامل
5. backtest_optimized.py  → باكتستنق محسّن (استخدم هذا!)
6. backtest_standalone.py → باكتستنق بسيط
7. binance_fetcher.py     → جلب من Binance مباشرة
8. live_monitor.py        → المراقبة الحية
9. main.py                → هذا البرنامج

════════════════════════════════════════════════════════════════════════
                      الأسئلة الشائعة
════════════════════════════════════════════════════════════════════════

Q: لماذا النتائج سيئة؟
A: • البيانات التجريبية عشوائية
   • جرّب بيانات حقيقية من Binance
   • أو عدّل المعاملات (EMA, RSI, ATR)

Q: كم صفقة تكون طبيعي؟
A: • Scalping (5m): 20-30 يومياً
   • Swing (15m-1h): 5-10 يومياً
   • Long-term (4h+): 1-3 يومياً

Q: ايش أفضل Profit Factor؟
A: • > 3.0 = ممتاز
   • > 2.0 = جيد جداً
   • > 1.5 = جيد
   • < 1.0 = خسارة

Q: هل الأداة آمنة للاستخدام الحي؟
A: • آمنة إذا ركّبت Stop Loss صحيح
   • ابدأ برأس مال صغير
   • راقب أول 10 صفقات بانتباه
   • تجنب الـ Over-leveraging

════════════════════════════════════════════════════════════════════════
                    نصائح مهمة أخرى
════════════════════════════════════════════════════════════════════════

💡 للحصول على نتائج أفضل:
   1. استخدم بيانات من Binance (لا تجريبية)
   2. اختبر على فترة 3-6 أشهر
   3. ركّب Telegram للتنبيهات
   4. ابدأ بتداول ورقي (paper trading)
   5. تابع الأخبار الاقتصادية

⚠️ أخطاء شائعة:
   ❌ الإفراط في التداول (overtrading)
   ❌ عدم احترام Stop Loss
   ❌ زيادة رأس المال بسرعة
   ❌ التداول في أوقات الأخبار المهمة
   ❌ عدم حفظ السجلات

════════════════════════════════════════════════════════════════════════
"""
    
    print(guide)
    input("\nاضغط Enter للعودة للقائمة...")
    return True


def print_footer():
    """طبع التذييل"""
    print("\n" + "="*70)
    print("شكراً لاستخدام أداة التداول الكريبتوجرافية! 🚀")
    print("="*70)
    print("\n💡 نصيحة أخيرة:")
    print("   الصبر والانضباط = مفتاح النجاح في التداول")
    print("   جرّب الباكتستنق اولاً قبل التداول الحي\n")


def main():
    """الحلقة الرئيسية"""
    
    print("\n" + "="*70)
    print("🎉 مرحبا بك في أداة التداول الكريبتوجرافية!")
    print("="*70)
    print("\nإصدار: 1.0")
    print(f"التاريخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("\nالمراحل المتاحة:")
    print("✅ 1. جلب البيانات")
    print("✅ 2. حساب المؤشرات")
    print("✅ 3. إشارات الدخول/الخروج")
    print("✅ 4. الباكتستنق (محسّن)")
    print("✅ 5. المراقبة الحية")
    print("✅ 6. التنبيهات")
    
    while True:
        print_menu()
        
        choice = input("اختيارك: ").strip()
        
        if choice == '1':
            success = option_1_optimized_backtest()
            if success:
                print("\n✅ انتهى الباكتستنق بنجاح!")
                print("➡️  نصيحة: اقرأ النتائج بعناية وعدّل المعاملات إذا لزم الأمر")
        
        elif choice == '2':
            success = option_2_simple_backtest()
            if success:
                print("\n✅ انتهى!")
        
        elif choice == '3':
            success = option_3_live_monitor()
        
        elif choice == '4':
            success = option_4_test_data_fetcher()
        
        elif choice == '5':
            success = option_5_test_indicators()
        
        elif choice == '6':
            success = option_6_guide()
        
        elif choice == '0':
            print_footer()
            break
        
        else:
            print("❌ اختيار غير صحيح، جرّب مجدداً")
        
        input("\nاضغط Enter للمتابعة...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⏹️  تم الإيقاف من قبل المستخدم")
        print_footer()
    except Exception as e:
        print(f"\n❌ خطأ: {e}")
        print_footer()
