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

## 📝 License
This project is proprietary and not open-source.
Contact the Eve team for licensing discussions.

---


