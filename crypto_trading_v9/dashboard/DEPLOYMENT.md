# النشر

## 1. Localhost — `LOCAL READY` ✅

```bash
DASHBOARD_ENVIRONMENT_TIER=local
DASHBOARD_API_HOST=127.0.0.1
./run.sh
```

المصادقة اختيارية · المفتاح السري اختياري · DEBUG مسموح.

المصادقة اختيارية. لا مفاتيح تداول. قراءة فقط.
الوصول محصور بالجهاز نفسه.

---

## 2. شبكة خاصة — `PRIVATE NETWORK READY` ✅ بشروط

الربط على عنوان غير محلي **يرفض الإقلاع** بلا مصادقة:

```
⛔ الربط على 0.0.0.0 بلا مصادقة. اضبط DASHBOARD_USERS
   أو DASHBOARD_TRUST_PROXY_AUTH=1 خلف وكيل، أو ابقَ على 127.0.0.1
```

### الخيار أ — مصادقة داخل Flask

```bash
# 1) المفتاح السري (إلزامي خارج local)
python3 -c "import sys;sys.path.insert(0,'backend');
import config;print(config.generate_secret())"

# 2) هاش كلمة المرور
python3 -c "import sys;sys.path.insert(0,'backend');
import security as s;print(s.hash_password('كلمتك'))"
```

```bash
DASHBOARD_ENVIRONMENT_TIER=private
DASHBOARD_SECRET_KEY=<المفتاح>
DASHBOARD_USERS=ops:pbkdf2_sha256$200000$...
DASHBOARD_API_HOST=0.0.0.0
DASHBOARD_HTTPS_ENABLED=1
DASHBOARD_CORS_ORIGIN=https://ops.internal
```

**فحص الإقلاع يرفض التشغيل** إن نقص المفتاح أو المصادقة أو HTTPS
(في production) أو كان `DEBUG` مفعّلاً أو `CORS=*`.

### تحديد المعدّل مع عدة عمّال

الحدود في الذاكرة — كل عامل يحسب مستقلاً. مع `gunicorn -w 4` يصير
الحد الفعلي أربعة أضعاف. الحل: تحديد المعدّل في الوكيل:

```nginx
limit_req_zone $binary_remote_addr zone=dash:10m rate=4r/s;
location / { limit_req zone=dash burst=20 nodelay; proxy_pass ...; }
```

### الخيار ب — وكيل عكسي (مفضَّل)

```nginx
server {
  listen 443 ssl;
  server_name dash.internal;
  ssl_certificate     /etc/ssl/dash.crt;
  ssl_certificate_key /etc/ssl/dash.key;

  auth_basic "Trading Dashboard";
  auth_basic_user_file /etc/nginx/.htpasswd;

  location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
  }
}
```

```bash
DASHBOARD_API_HOST=127.0.0.1      # اربط محلياً فقط
DASHBOARD_TRUST_PROXY_AUTH=1
```

مع خادم WSGI حقيقي:

```bash
pip install gunicorn
gunicorn -w 2 -b 127.0.0.1:8080 --chdir backend 'app:create_app()'
```

---

## 3. الإنترنت العام — `NOT READY` ⛔

**غير مدعوم في هذه النسخة.** ينقص:

- TLS مُدار وتجديد شهادات
- SSO أو OIDC (Basic Auth لا تكفي علناً)
- Rate limiting على مستوى الحافة (WAF)
- مراقبة وتنبيه واستجابة للحوادث
- تحديثات أمنية دورية
- تدقيق خارجي

لا تنشرها علناً بلا هذه.

---

## قائمة تحقق قبل أي نشر غير محلي

- [ ] `DASHBOARD_USERS` أو `DASHBOARD_TRUST_PROXY_AUTH`
- [ ] TLS + `DASHBOARD_SECURE_COOKIES=1`
- [ ] `DASHBOARD_CORS_ORIGIN` محدد (لا `*`)
- [ ] خادم WSGI حقيقي
- [ ] القاعدة على قرص يقرأه المستخدم فقط
- [ ] لا مفاتيح تداول في بيئة اللوحة
- [ ] مراجعة سجل الوصول
