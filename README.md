# Eve – Virtual Reality Freedom Platform

Eve is a Django-based web application designed to bring immersive VR experiences to long-term patients. The goal is to provide freedom, mobility, and emotional comfort through virtual environments that can be explored from a hospital bed or any limited-mobility setting.

This GitHub-optimized README includes:
- Project overview
- Features
- Tech stack
- Architecture summary
- Local development setup
- Environment configuration
- Database setup
- Running the application locally

---

## 🌌 Project Overview
Eve provides a modular and scalable VR-focused e-commerce and user platform. Long-term patients can browse VR experiences, purchase them securely, and interact with caregivers or staff.

The platform integrates:
- **User authentication** (register/login/logout)
- **User profiles**
- **Product catalogue via Saleor (GraphQL)**
- **Product details**
- **MongoDB-backed cart**
- **Checkout & payment history scaffolding**

---

## ✨ Core Features
### ✔ Landing Page
A warm introduction to Eve’s mission and VR solutions.

### ✔ User Accounts
- Register
- Login / Logout
- User profile page

### ✔ Product Catalogue
- Powered by **Saleor Cloud** (GraphQL API)
- Cached in MongoDB for fast loading

### ✔ Product Detail Pages
- Dynamic content fetched from Saleor
- Media gallery
- Pricing information

### ✔ Shopping Cart
- Stored in MongoDB (`carts` collection)
- Add/remove products

### ✔ Ready for Checkout & Payments
Core structure prepared for integration with Stripe or Saleor Checkout.

---

## 🛠 Tech Stack
**Backend:** Django 5.2, Django REST Framework

**Databases:**
- **PostgreSQL** – User accounts, orders
- **MongoDB** – Product cache, cart, analytics

**E-Commerce Backend:**
- **Saleor Cloud** via GraphQL

**Static Files:**
- WhiteNoise

**Other:**
- Python Decouple for environment variables
- Gunicorn for WSGI

---

## 🏗 Architecture Summary
### **Django MVC Structure**
- `core/` – Landing page & shared templates
- `accounts/` – Authentication & profile
- `ecommerce/` – Catalogue, product details, cart
- `payments/` – Payment flow scaffolding

### **MongoDB Collections**
- `products_cache` – Cached Saleor products
- `carts` – User shopping carts
- `usage_logs` – Optional analytics events

### **Saleor Cloud Integration**
The app consumes a headless Saleor GraphQL API.
Products are:
1. Pulled from Saleor
2. Cached in MongoDB
3. Served to the frontend

This avoids repeated Saleor requests while keeping data fresh.

---

## ⚙️ Local Development Setup
Follow these steps to run the project on your machine.

### 1. Clone the Repository
```bash
git clone <REPOSITORY_URL>
cd Eve
```

### 2. Create a Virtual Environment
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Create a `.env` File
Create `.env` in the project root with the following structure:

```
DJANGO_SECRET_KEY=dev-secret-key-change-me
DJANGO_SETTINGS_MODULE=eve.settings.dev

# PostgreSQL
DB_NAME=eve_db
DB_USER=eve_user
DB_PASSWORD=eve_password
DB_HOST=localhost
DB_PORT=5432

# MongoDB
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB_NAME=eve_ecommerce

# Saleor GraphQL
SALEOR_GRAPHQL_URL=https://your-env.eu.saleor.cloud/graphql/
SALEOR_CHANNEL=default-channel
SALEOR_API_TOKEN=your-api-token
```

> Ensure `.env` is listed in `.gitignore`.

---

## 🗄 Database Setup

### PostgreSQL Setup
```bash
psql -U postgres
```
Inside the PostgreSQL shell:
```sql
CREATE DATABASE eve_db;
CREATE USER eve_user WITH PASSWORD 'eve_password';
GRANT ALL PRIVILEGES ON DATABASE eve_db TO eve_user;
```

### MongoDB Setup
If running locally:
```bash
mongosh
use eve_ecommerce
```
If using MongoDB Atlas, use the connection URI in your `.env`.

---

## ▶️ Running Eve Locally
### 1. Apply Migrations
```bash
python manage.py migrate
```

### 2. Create Superuser
```bash
python manage.py createsuperuser
```

### 3. Start the Development Server
```bash
python manage.py runserver
```

Now visit `http://127.0.0.1:8000/`.

You should be able to:
- Register & login
- Browse the Saleor-powered product catalogue
- View product details
- Add/remove items from the Mongo-backed cart

---

# Threat Accessment:

### High-Level Data Flow & Trust Boundaries
```
Conceptual DFD in text:

Browser (Public Internet)

Sends HTTP(S) requests to Django app (views / DRF endpoints).

Django App Server (Trust Boundary #1)

Authenticates users

Reads/writes to PostgreSQL (users, orders)

Reads/writes to MongoDB (products cache, carts, logs)

Calls Saleor GraphQL over HTTPS with API token.

PostgreSQL DB (trusted network, more restricted)

MongoDB (trusted network, must be locked down—common misconfig)

Saleor Cloud (external SaaS, Trust Boundary #2)

Static files served via WhiteNoise / CDN

Boundaries to watch:

Internet ⇄ Django

Django ⇄ PostgreSQL

Django ⇄ MongoDB

Django ⇄ Saleor Cloud (GraphQL over internet)

```
---

## STRIDE-Based Threats by Area

### Grouped by component and call out risk + mitigations

```

### Threats

- Spoofing / Account takeover
  - Credential stuffing & password spraying.
  - Weak password policy or no rate limiting on login.

- Session hijacking
  - Session IDs in non-Secure cookies, not HttpOnly, or too long-lived.

- Broken auth flows (OWASP A07 – Authentication Failures)
    -Cloudflare

  - CSRF issues on login/logout/critical actions.
  - Insecure password reset flow (guessable tokens, token reuse, no expiry).

- Privilege escalation
  - Normal user accessing staff/admin features (IDOR or missing permission checks).

### Mitigations

- Enforce strong password policy + password complexity & minimum length. (On-going)
- Enable rate limiting on login & password reset (e.g. django-axes or custom throttling). (on-going)
- Ensure Django Session / CSRF middleware enabled and correctly configured. ✔ 
- Cookies: Secure, HttpOnly, SameSite=Lax or Strict where possible. (on-going, WIP)
- Use Django’s built-in auth & permission system; restrict staff-only views with: ✔ 
- @login_required, @permission_required, DRF IsAuthenticated, IsAdminUser etc. ✔ 

Lock down password reset flows:
- Random, single-use, short-lived tokens.
- No leaking whether an email is registered (generic error messages).

Optional: integrate 2FA for staff/admin accounts.

```
### Access Control & Multi-Tenancy (Patients vs Staff)
Threats
- Broken Access Control (OWASP A01)
  - Users can access or modify others’ profiles, carts, or orders by guessing IDs.
  - Patients can access staff/admin endpoints by manipulating URLs or form fields.
  - Unprotected DRF endpoints returning sensitive lists (all users, all carts).

Mitigations
  
- Always scope queries to the current user:
  - Cart.objects.filter(user=request.user) rather than arbitrary IDs.

- Use object-level permissions (DRF BasePermission / DjangoModelPermissions).

- For staff/admin views, enforce:
  - @user_passes_test(lambda u: u.is_staff) or role-based flags.

- Perform access checks on the server (never trust client-side filtering).

- Avoid exposing raw DB IDs when possible; use UUIDs or opaque identifiers.


### Product Catalogue & Saleor GraphQL Integration
Threats
- Tampering / Data integrity
  - Attacker hits Saleor directly or intercepts GraphQL traffic (if misconfigured) to manipulate product data your app trusts.
  - Your app might trust unvalidated fields from Saleor, leading to HTML/JS injection in product descriptions.

- Information disclosure
  - Overly broad GraphQL queries exposing internal data.
  - GraphQL introspection enabled in production, leak schema details.

- DoS
  - Expensive or nested GraphQL queries causing slowdowns in Saleor & your app.
 
Mitigations

- Enforce server-side validation/sanitization of any HTML from Saleor (e.g. bleach/allowlist) before rendering in templates.

- Configure Saleor endpoint to require HTTPS and valid API token; rotate tokens periodically.

- Limit GraphQL queries in your app:
  - Fixed whitelisted queries instead of dynamically passing user-controlled query strings.

- Enable GraphQL depth/complexity limiting on the Saleor side if possible.

- Cache responses safely in MongoDB with validation of expected schema.


### MongoDB:
Threats

- Data exposure
  -MongoDB exposed directly to the internet (common misconfig).

- Tampering
  - If compromised, attacker can manipulate carts, change prices, etc.

- Injection
  - If user-supplied data is used directly in Mongo queries (NoSQL injection).

Mitigations

- Ensure MongoDB is not exposed publicly:
  - Bind to internal network / localhost only.
  - Use strong auth & network-level controls (VPC, security groups, firewall).

- Treat cart & pricing as server-side trusted data:
  - Cart items stored by product ID and quantity; never trust client-sent price.
  - Recalculate price from Saleor/PostgreSQL on checkout.

- Validate and sanitize all Mongo query parameters (no direct $where or unsanitized operators).
- Use least privilege Mongo user (no admin on entire cluster).

### PostgreSQL: User Accounts & Orders
Threats

  - SQL Injection (OWASP A05: Injection)
  - Data exposure
      - Direct DB compromise leaks user profiles, order history.

- Tampering / integrity loss
    - Attacker alters orders, entitlements, or user roles.

Mitigations

- Use Django ORM everywhere; avoid raw SQL when possible. ✔
- If you must use raw SQL, parameterize queries.
- Separate DB user for Django with limited privileges; no superuser / owner where unnecessary. ✔
- Enforce strong DB auth and restrict network access to DB from app servers only. ✔
- Regular backups + tested restore process (threat: ransomware / data loss).

### Shopping Cart & Checkout Flow
Threats

- Price manipulation
  - Attacker changes price in client-side forms or API calls.
- Quantity / stock abuse
  - Cart holds unrealistic quantities, manipulating inventory or analytics.
- Replay attacks
  - Reusing old checkout URLs or tokens.

Mitigations

- Treat all pricing & discounts as server-side: ✔
  - On checkout: fetch product prices from trusted source (Saleor / DB) and ignore any client-supplied price. ✔
- For future payments (Stripe/Saleor): 
  - Use server-created payment intents; amount derived server-side. ✔
  - Validate payment webhook signatures and match to internal order IDs. 
- Limit max quantities per product per order and per cart.
- Use one-time, time-limited order/checkout tokens.

### Static Files, VR Media, and Content
Threats

- XSS & content injection
  - User-entered content (names, messages, etc.) displayed without escaping.
- Clickjacking
  - App framed by attacker site, capturing user actions.
- Open redirects
  - Misused next parameters / redirect URLs.

Mitigations

- Escape output in templates by default (Django does this; avoid |safe unless absolutely necessary).
- Add security headers:
  - X-Frame-Options: `DENY` or `SAMEORIGIN`
  - Content-Security-Policy tuned to your static & media hosts.
- Validate redirect targets (only internal paths, or strict allowlist).

### Configuration, Secrets, & Deployment

 - Secrets leakage ✔
  - `.env` accidentally committed  ✔


- Security Misconfiguration (OWASP A02)

   - `DEBUG=True` in production. ✔
   - Misconfigured ALLOWED_HOSTS. ✔
   - Un-hardened Gunicorn / reverse proxy settings.
     
 - Vulnerable or outdated components 
  - Old Django/DRF/requests libraries with known CVEs (OWASP A06).


Mitigations
- Use environment variables + `.env` (already in README) and a proper secrets manager in production (AWS SSM, Vault, etc.). ✔
- Separate dev vs prod settings modules:
  - `DEBUG=False` in prod ✔
  - Strong `SECRET_KEY` only set in prod environment ✔
  - Correct `ALLOWED_HOSTS`, `SECURE_*` settings (HSTS, SSL redirect, etc.). ✔
- Regular dependency scanning:
  - `pip-audit`, `pip-tools`, Dependabot, or similar.
- Containerize with minimal base image, non-root runtime user if using Docker.

### Logging, Monitoring, & Incident Response

Threats
- Repudiation
  - Users deny actions (e.g. “I never bought that VR experience”) and you can’t verify.
- Delayed detection
  - Attacks (brute-force, scraping, tampering) go unnoticed due to weak logging and no alerts.

Mitigations
- Log:
  - Auth events (login success/fail, password reset, new devices).
  - Cart changes, order creation, admin actions.
  - Saleor API errors & anomalies.
- Ensure logs contain:
  - Timestamp, user ID (or anonymous marker), request path, IP (respecting privacy/reg laws).
- Centralize logs & add detection:
  - Threshold alerts on failed logins, unusual cart actions, etc.

---

## 📝 License
This project is proprietary and not open-source.
Contact the Eve team for licensing discussions.

---


