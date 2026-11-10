#!/usr/bin/env python3
"""Generate large test datasets:  python3 scripts/generate_data.py --users 50000 --products 20000"""
import argparse, csv, random
ap = argparse.ArgumentParser()
ap.add_argument("--users", type=int, default=1000)
ap.add_argument("--products", type=int, default=500)
ap.add_argument("--out", default="data")
a = ap.parse_args()
random.seed(42)
with open(f"{a.out}/users.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["username", "password"])
    for i in range(1, a.users + 1): w.writerow([f"user{i:06d}", f"Passw0rd!{i:06d}"])
with open(f"{a.out}/products.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["product_id", "sku", "price"])
    for i in range(1, a.products + 1): w.writerow([100 + i, f"SKU-{100 + i}", round(random.uniform(5, 500), 2)])
print("generated", a.users, "users and", a.products, "products in", a.out)
