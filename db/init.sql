CREATE TABLE IF NOT EXISTS orders (
  id SERIAL PRIMARY KEY, customer TEXT NOT NULL, status TEXT NOT NULL,
  total NUMERIC(10,2) NOT NULL, created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS idx_orders_customer_created ON orders (customer, created_at DESC);
CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY, user_name TEXT, action TEXT, created_at TIMESTAMPTZ DEFAULT now());
INSERT INTO orders (customer, status, total)
SELECT 'user' || lpad(((g % 500) + 1)::text, 4, '0'),
       (ARRAY['CONFIRMED','SHIPPED','PENDING'])[1 + (g % 3)], round((random()*500)::numeric, 2)
FROM generate_series(1, 50000) g;
