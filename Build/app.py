import os
import random
import time
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor
from jose import JWTError, jwt
from datetime import datetime, timedelta
import asyncio

# ---- Конфигурация ----
SECRET_KEY = "secret-key-for-testing-only"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

INSTANCE_ID = os.getenv("INSTANCE_ID", "unknown")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://perf_user:perf_pass@localhost:5432/perf_shop")

app = FastAPI(title="Performance Shop API", version="1.0")

# ---- Модели данных ----
class LoginRequest(BaseModel):
    username: str
    password: str

class OrderCreate(BaseModel):
    product_id: int
    quantity: int

# ---- Подключение к БД ----
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"DB connection error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

# ---- Вспомогательные функции ----
def get_user_by_username(username: str):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Header(...)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ---- ЭНДПОИНТЫ ----

# 1. POST /api/auth/login — авторизация (без хеширования!)
@app.post("/api/auth/login")
async def login(request: LoginRequest):
    user = get_user_by_username(request.username)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    # ⚠️ Пароль сравнивается напрямую (без шифрования)
    if request.password != user["password_hash"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    access_token = create_access_token(
        data={"sub": user["username"]},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_id": user["id"],
        "instance": INSTANCE_ID
    }

# 2. GET /api/auth/profile — профиль (требует токен)
@app.get("/api/auth/profile")
async def get_profile(username: str = Depends(get_current_user)):
    return {
        "username": username,
        "instance": INSTANCE_ID,
        "timestamp": datetime.utcnow().isoformat()
    }

# 3. GET /api/products — список товаров
@app.get("/api/products")
async def get_products():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products ORDER BY id")
    products = cur.fetchall()
    cur.close()
    conn.close()
    return {
        "instance": INSTANCE_ID,
        "count": len(products),
        "products": products
    }

# 4. GET /api/products/{id} — товар по ID
@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id = %s", (product_id,))
    product = cur.fetchone()
    cur.close()
    conn.close()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return {
        "instance": INSTANCE_ID,
        "product": product
    }

# 5. POST /api/orders — создание заказа
@app.post("/api/orders")
async def create_order(order: OrderCreate):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM products WHERE id = %s", (order.product_id,))
    product = cur.fetchone()
    if not product:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Product not found")
    
    total_price = product["price"] * order.quantity
    cur.execute(
        "INSERT INTO orders (product_id, quantity, total_price, status) VALUES (%s, %s, %s, %s) RETURNING id",
        (order.product_id, order.quantity, total_price, "pending")
    )
    order_id = cur.fetchone()["id"]
    
    cur.execute("UPDATE products SET stock = stock - %s WHERE id = %s", (order.quantity, order.product_id))
    conn.commit()
    cur.close()
    conn.close()
    
    return {
        "instance": INSTANCE_ID,
        "order_id": order_id,
        "total_price": total_price,
        "status": "pending"
    }

# 6. GET /api/orders/{id} — заказ по ID
@app.get("/api/orders/{order_id}")
async def get_order(order_id: int):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT o.*, p.name as product_name, p.price 
        FROM orders o 
        JOIN products p ON o.product_id = p.id 
        WHERE o.id = %s
    """, (order_id,))
    order = cur.fetchone()
    cur.close()
    conn.close()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return {
        "instance": INSTANCE_ID,
        "order": order
    }

# 7. GET /api/reports/sales — отчёт по продажам (тяжёлый)
@app.get("/api/reports/sales")
async def sales_report():
    time.sleep(1.5)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("""
        SELECT 
            p.id as product_id,
            p.name,
            COUNT(o.id) as orders_count,
            SUM(o.total_price) as total_revenue,
            AVG(o.total_price) as avg_order_value
        FROM products p
        LEFT JOIN orders o ON p.id = o.product_id
        GROUP BY p.id, p.name
        ORDER BY total_revenue DESC NULLS LAST
    """)
    report = cur.fetchall()
    cur.close()
    conn.close()
    
    time.sleep(1.0)
    
    return {
        "instance": INSTANCE_ID,
        "report_type": "sales_summary",
        "generated_at": datetime.utcnow().isoformat(),
        "data": report
    }

# 8. GET /api/reports/slow — искусственно медленный эндпоинт
@app.get("/api/reports/slow")
async def slow_report(delay: int = 3):
    await asyncio.sleep(delay)
    return {
        "instance": INSTANCE_ID,
        "message": f"Slow response after {delay} seconds",
        "timestamp": datetime.utcnow().isoformat()
    }

# 9. POST /api/reports/generate — генерация тяжёлого отчёта
@app.post("/api/reports/generate")
async def generate_report(period: str = "month"):
    delay = random.randint(3, 7)
    await asyncio.sleep(delay)
    return {
        "instance": INSTANCE_ID,
        "period": period,
        "status": "completed",
        "execution_time": delay,
        "generated_at": datetime.utcnow().isoformat()
    }

# 10. GET /api/health — проверка здоровья
@app.get("/api/health")
async def health():
    return {
        "status": "healthy",
        "instance": INSTANCE_ID,
        "timestamp": datetime.utcnow().isoformat()
    }

# ---- Корневой эндпоинт ----
@app.get("/")
async def root():
    return {
        "service": "Performance Shop API",
        "instance": INSTANCE_ID,
        "docs": "/docs",
        "status": "running"
    }