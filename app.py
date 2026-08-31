
import os, json, sqlite3, socket
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "orders.db")
DEFAULT_PRODUCTS = json.load(open(os.path.join(BASE, "products.json"), encoding="utf-8"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY","change-me-before-production")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD","hcburger")
PRINTER_IP = os.getenv("PRINTER_IP","")
PRINTER_PORT = int(os.getenv("PRINTER_PORT","9100"))
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY","")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL","http://localhost:8000")

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    con=db()
    con.execute("""CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL,
        customer_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        order_type TEXT NOT NULL,
        address TEXT,
        payment TEXT NOT NULL,
        payment_status TEXT NOT NULL DEFAULT 'unpaid',
        note TEXT,
        total REAL NOT NULL,
        items_json TEXT NOT NULL
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        price REAL NOT NULL,
        menu_price REAL,
        image TEXT,
        active INTEGER NOT NULL DEFAULT 1
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""")
    count=con.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
    if count==0:
        for p in DEFAULT_PRODUCTS:
            con.execute("""INSERT INTO products(category,name,description,price,menu_price,image,active)
                           VALUES(?,?,?,?,?,?,1)""",(p["cat"],p["name"],p["desc"],p["price"],p["menu"],p["img"]))
    defaults={
        "restaurant_name":"HC BURGER FRAIS",
        "address":"24 Boulevard Banon, 13004 Marseille",
        "phone":"04 91 49 38 68",
        "hours_1":"Lun–Jeu & Sam : 11:00 → 00:00",
        "hours_2":"Ven & Dim : 14:00 → 00:00",
        "delivery_postcodes":"13004",
        "delivery_fee":"0",
        "delivery_label":"Livraison gratuite dans le 13004",
        "allow_sur_place":"1",
        "allow_takeaway":"1",
        "allow_delivery":"1",
        "allow_pay_restaurant":"1",
        "allow_pay_online":"1"
    }
    for k,v in defaults.items():
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
    con.commit(); con.close()

def get_settings():
    con=db(); rows=con.execute("SELECT key,value FROM settings").fetchall(); con.close()
    return {r["key"]:r["value"] for r in rows}

def get_products(active_only=True):
    con=db()
    q="SELECT * FROM products"
    if active_only: q+=" WHERE active=1"
    q+=" ORDER BY category,name"
    rows=[dict(r) for r in con.execute(q).fetchall()]
    con.close()
    return rows

def admin_required(fn):
    @wraps(fn)
    def inner(*a,**kw):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return fn(*a,**kw)
    return inner

def money(x): return f"{x:.2f} €".replace(".",",")

def ticket_text(o):
    items=json.loads(o["items_json"])
    s=get_settings()
    lines=[s["restaurant_name"],s["address"],s["phone"],"-"*32,
           f"COMMANDE #{o['id']}",o["created_at"],"-"*32]
    for it in items:
        lines.append(f"{it['qty']}x {it['name']} ({it['kind']})")
        lines.append(f"   {money(it['unit']*it['qty'])}")
    lines += ["-"*32,f"TOTAL: {money(o['total'])}",o["order_type"],
              f"Client: {o['customer_name']}",f"Tel: {o['phone']}"]
    if o["address"]: lines.append(f"Adresse: {o['address']}")
    if o["note"]: lines.append(f"Note: {o['note']}")
    lines += ["",""]
    return "\n".join(lines)

def try_network_print(o):
    if not PRINTER_IP:
        return False, "Adresse IP imprimante non configurée"
    data=ticket_text(o).encode("cp858",errors="replace")+b"\n\n\n\x1dV\x00"
    try:
        with socket.create_connection((PRINTER_IP,PRINTER_PORT),timeout=3) as sock:
            sock.sendall(data)
        return True,"Ticket envoyé à l'imprimante"
    except Exception as e:
        return False,str(e)

@app.route("/")
def home():
    return render_template("index.html", products=get_products(), settings=get_settings())

@app.post("/api/orders")
def create_order():
    data=request.get_json(force=True)
    items=data.get("items") or []
    if not items: return jsonify(ok=False,error="Panier vide"),400
    s=get_settings()
    name=(data.get("name") or "").strip()
    phone=(data.get("phone") or "").strip()
    typ=data.get("type")
    address=(data.get("address") or "").strip()
    postcode=(data.get("postcode") or "").strip()
    payment=data.get("payment")
    note=(data.get("note") or "").strip()
    allowed=[]
    if s.get("allow_sur_place")=="1": allowed.append("Sur place")
    if s.get("allow_takeaway")=="1": allowed.append("À emporter")
    if s.get("allow_delivery")=="1": allowed.append("Livraison")
    if not name or not phone: return jsonify(ok=False,error="Nom et téléphone requis"),400
    if typ not in allowed: return jsonify(ok=False,error="Type de commande non disponible"),400
    if typ=="Livraison":
        postcodes=[x.strip() for x in s.get("delivery_postcodes","").split(",") if x.strip()]
        if postcode not in postcodes:
            return jsonify(ok=False,error="Livraison non disponible pour ce code postal"),400
        if not address: return jsonify(ok=False,error="Adresse requise"),400

    products=get_products()
    price_map={}
    for p in products:
        price_map[(p["name"],"Seul")]=float(p["price"])
        if p["menu_price"] is not None:
            price_map[(p["name"],"Menu")]=float(p["menu_price"])
    clean=[]; total=0
    for it in items:
        key=(it.get("name"),it.get("kind"))
        if key not in price_map: return jsonify(ok=False,error="Produit invalide"),400
        qty=max(1,min(20,int(it.get("qty",1))))
        unit=price_map[key]
        total+=unit*qty
        clean.append({"name":key[0],"kind":key[1],"qty":qty,"unit":unit})
    if typ=="Livraison":
        total += float(s.get("delivery_fee","0") or 0)

    con=db()
    cur=con.execute("""INSERT INTO orders(created_at,status,customer_name,phone,order_type,address,payment,payment_status,note,total,items_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"new",name,phone,typ,
                     (address+" "+postcode).strip(),payment,"unpaid",note,total,json.dumps(clean,ensure_ascii=False)))
    oid=cur.lastrowid; con.commit()
    o=con.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    printed,msg=try_network_print(o)
    con.close()

    if payment=="Paiement CB en ligne":
        if not STRIPE_SECRET_KEY:
            return jsonify(ok=True,order_id=oid,total=total,printed=printed,print_message=msg,
                           payment_required=True,payment_ready=False,
                           message="Commande enregistrée. Le compte de paiement doit encore être connecté.")
        try:
            import stripe
            stripe.api_key=STRIPE_SECRET_KEY
            checkout=stripe.checkout.Session.create(
                mode="payment",
                line_items=[{
                    "price_data":{"currency":"eur","product_data":{"name":f"Commande HC Burger #{oid}"},"unit_amount":int(round(total*100))},
                    "quantity":1
                }],
                success_url=f"{PUBLIC_BASE_URL}/payment/success?order_id={oid}&session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{PUBLIC_BASE_URL}/payment/cancel?order_id={oid}",
                metadata={"order_id":str(oid)}
            )
            return jsonify(ok=True,order_id=oid,total=total,printed=printed,checkout_url=checkout.url,payment_ready=True)
        except Exception as e:
            return jsonify(ok=True,order_id=oid,total=total,printed=printed,payment_ready=False,
                           message=f"Commande enregistrée mais paiement non lancé: {e}")
    return jsonify(ok=True,order_id=oid,total=total,printed=printed,print_message=msg,payment_ready=True)

@app.get("/payment/success")
def payment_success():
    oid=request.args.get("order_id")
    sid=request.args.get("session_id")
    if oid and sid and STRIPE_SECRET_KEY:
        try:
            import stripe
            stripe.api_key=STRIPE_SECRET_KEY
            sess=stripe.checkout.Session.retrieve(sid)
            if sess.payment_status=="paid":
                con=db(); con.execute("UPDATE orders SET payment_status='paid' WHERE id=?",(oid,)); con.commit(); con.close()
        except Exception:
            pass
    return render_template("payment_result.html", ok=True, order_id=oid)

@app.get("/payment/cancel")
def payment_cancel():
    return render_template("payment_result.html", ok=False, order_id=request.args.get("order_id"))

@app.route("/admin/login",methods=["GET","POST"])
def admin_login():
    if request.method=="POST":
        if request.form.get("password")==ADMIN_PASSWORD:
            session["admin"]=True
            return redirect(url_for("admin"))
        return render_template("login.html",error="Mot de passe incorrect")
    return render_template("login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear(); return redirect(url_for("admin_login"))

@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html",settings=get_settings())

@app.get("/api/admin/orders")
@admin_required
def admin_orders():
    con=db(); rows=con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 100").fetchall(); con.close()
    out=[]
    for r in rows:
        d=dict(r); d["items"]=json.loads(d.pop("items_json"))
        out.append(d)
    return jsonify(out)
@app.post("/api/admin/orders/<int:oid>/status")
@admin_required
def admin_status(oid):
    st=request.json.get("status")
    if st not in ["accepted","rejected","done"]: return jsonify(ok=False),400
    con=db(); con.execute("UPDATE orders SET status=? WHERE id=?",(st,oid)); con.commit(); con.close()
    return jsonify(ok=True)

@app.post("/api/admin/orders/<int:oid>/print")
@admin_required
def admin_print(oid):
    con=db(); o=con.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not o: con.close(); return jsonify(ok=False,error="Introuvable"),404
    ok,msg=try_network_print(o); con.close()
    return jsonify(ok=ok,message=msg)

@app.route("/admin/products",methods=["GET","POST"])
@admin_required
def admin_products():
    if request.method=="POST":
        f=request.form
        con=db()
        if f.get("id"):
            con.execute("""UPDATE products SET category=?,name=?,description=?,price=?,menu_price=?,image=?,active=? WHERE id=?""",
                        (f["category"],f["name"],f.get("description",""),float(f["price"]),
                         float(f["menu_price"]) if f.get("menu_price") else None,
                         f.get("image",""),1 if f.get("active")=="on" else 0,int(f["id"])))
        else:
            con.execute("""INSERT INTO products(category,name,description,price,menu_price,image,active)
                           VALUES(?,?,?,?,?,?,?)""",
                        (f["category"],f["name"],f.get("description",""),float(f["price"]),
                         float(f["menu_price"]) if f.get("menu_price") else None,
                         f.get("image",""),1))
        con.commit();con.close()
        return redirect(url_for("admin_products"))
    return render_template("products.html",products=get_products(False))

@app.post("/admin/products/<int:pid>/delete")
@admin_required
def delete_product(pid):
    con=db(); con.execute("DELETE FROM products WHERE id=?",(pid,)); con.commit(); con.close()
    return redirect(url_for("admin_products"))

@app.route("/admin/settings",methods=["GET","POST"])
@admin_required
def admin_settings():
    if request.method=="POST":
        con=db()
        keys=["restaurant_name","address","phone","hours_1","hours_2","delivery_postcodes","delivery_fee","delivery_label"]
        for k in keys:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,request.form.get(k,"")))
        for k in ["allow_sur_place","allow_takeaway","allow_delivery","allow_pay_restaurant","allow_pay_online"]:
            val="1" if request.form.get(k)=="on" else "0"
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,val))
        con.commit();con.close()
        return redirect(url_for("admin_settings"))
    return render_template("settings.html",s=get_settings())

@app.get("/ticket/<int:oid>")
@admin_required
def ticket(oid):
    con=db(); o=con.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone(); con.close()
    if not o: return "Introuvable",404
    return render_template("ticket.html",o=o,items=json.loads(o["items_json"]))
@app.get("/cart")
def cart():
    cart_data = session.get("cart", {})
    items = []

    con = db()

    for pid, quantity in cart_data.items():
        product = con.execute(
            "SELECT * FROM products WHERE id = ?",
            (int(pid),)
        ).fetchone()

        if product:
            items.append({
                "id": product["id"],
                "name": product["name"],
                "price": float(product["price"]),
                "quantity": quantity
            })

    con.close()

    total = sum(item["price"] * item["quantity"] for item in items)

    return render_template("cart.html", items=items, total=total)
@app.get("/cart/remove/<int:pid>")
def remove_from_cart(pid):
    cart = session.get("cart", {})
    key = str(pid)

    if key in cart:
        if cart[key] > 1:
            cart[key] -= 1
        else:
            del cart[key]

        session["cart"] = cart

    return redirect(url_for("cart"))
@app.get("/cart/add/<int:pid>")
def add_to_cart(pid):
    cart = session.get("cart", {})
    key = str(pid)
    cart[key] = cart.get(key, 0) + 1
    session["cart"] = cart
    return redirect(url_for("cart"))
@app.route("/order/<int:pid>", methods=["GET", "POST"])
def order(pid):
    con = db()
    product = con.execute(
        "SELECT * FROM products WHERE id=?",
        (pid,)
    ).fetchone()

    if not product:
        con.close()
        return "Produit introuvable", 404

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        quantity = int(request.form.get("quantity", 1))

        formula = request.form.get("formula", "seul")
        drink = request.form.get("drink", "")

        unit_price = float(product["price"])
        item_name = product["name"]

    if formula == "menu":
       unit_price += 2.50
       item_name = f'{product["name"]} - Menu ({drink})'

       total = unit_price * quantity

       items = [{
    "product_id": pid,
    "name": item_name,
    "quantity": quantity,
    "price": unit_price
        }]
      con.execute(
            """INSERT INTO orders
            (created_at, status, customer_name, phone, order_type,
             address, payment, payment_status, note, total, items_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                "pending",
                name,
                phone,
                "takeaway",
                "",
                "cash",
                "unpaid",
                "",
                total,
                json.dumps(items)
            )
        )
        con.commit()
        con.close()
        return redirect(url_for("home"))
init_db()
if __name__=="__main__":
    
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8000")),debug=False)
