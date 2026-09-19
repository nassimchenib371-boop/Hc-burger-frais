
import os, json, sqlite3, socket

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, render_template, request, jsonify, session, redirect, url_for

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "orders.db")

# Zones de livraison HC Burger Frais.
# 13004 : gratuite dès 10 €, sinon 3 €.
# 13003/13005 : gratuite dès 15 €, sinon 3 €.
# 13012/13013/13014 : gratuite dès 20 €, sinon 3 €.
DELIVERY_RULES = {
    "13003": 15.0,
    "13004": 10.0,
    "13005": 15.0,
    "13012": 20.0,
    "13013": 20.0,
    "13014": 20.0,
}
DELIVERY_FEE_BELOW_THRESHOLD = 3.0

def delivery_fee_for(postcode, merchandise_total):
    threshold = DELIVERY_RULES.get((postcode or "").strip())
    if threshold is None:
        return None
    return 0.0 if float(merchandise_total) >= threshold else DELIVERY_FEE_BELOW_THRESHOLD
DEFAULT_PRODUCTS = json.load(open(os.path.join(BASE, "products.json"), encoding="utf-8"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY","change-me-before-production")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD","hcburger")
PRINTER_IP = os.getenv("PRINTER_IP","")
PRINTER_PORT = int(os.getenv("PRINTER_PORT","9100"))
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY","")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL","http://localhost:8000")
PARIS_TZ = ZoneInfo("Europe/Paris")

# Offre découverte : dès 2 menus dans le panier, 1 produit offert au choix.
PROMO_GIFTS = ["Tacos M", "Simple Smash", "Sandwich Kebab", "Pâtes à la crème"]
# Fidélité : 5 commandes avec exactement 1 menu Burger/Tacos/Sandwich = 6e menu offert.
LOYALTY_GIFTS = {
    "Menu Simple Smash": "Simple Smash",
    "Menu Big Burger": "Big Burger",
    "Menu Big Philippe": "Big Philly Cheese",
    "Menu Big Polo": "Big Pollo",
    "Menu Sandwich Poulet Mariné": "Sandwich Poulet Mariné",
    "Menu Sandwich Steak Cheddar": "Sandwich Steak Cheddar",
    "Menu Sandwich Kebab": "Sandwich Kebab",
    "Menu Sandwich Escalope": "Sandwich Escalope",
    "Menu Poutine Campagnard": "Poutine Campagna",
    "Pâtes à la crème": "Pâtes à la crème",
    "Pâtes à la crème Viande hachée": "Pâtes à la crème - Viande hachée",
    "Pâtes à la crème Poulet": "Pâtes à la crème - Poulet",
    "Menu Tacos": "Tacos M",
}
def restaurant_is_open():
    # Ouverture/fermeture manuelle depuis la page Administration.
    # La valeur est stockée en base et reste donc active après fermeture du navigateur.
    try:
        return get_settings().get("restaurant_open", "1") == "1"
    except Exception:
        return True
class PostgresConnection:
    """Petit adaptateur pour garder le code existant compatible avec Neon/PostgreSQL."""
    def __init__(self, conn):
        self.conn = conn

    @staticmethod
    def _sql(query):
        query = query.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        query = query.replace("?", "%s")
        # Les INSERT OR IGNORE SQLite deviennent ON CONFLICT DO NOTHING dans PostgreSQL.
        if "INSERT INTO" in query.upper() and "OR IGNORE" not in query.upper():
            original_markers = ("loyalty_events",)
            if any(marker in query for marker in original_markers) and "ON CONFLICT" not in query.upper():
                query = query.rstrip() + " ON CONFLICT DO NOTHING"
        return query

    def execute(self, query, params=()):
        return self.conn.execute(self._sql(query), params)

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        return self.conn.close()

def db():
    # Base locale rapide : commandes, produits et réglages restent en SQLite.
    # Neon n'est plus consulté pour charger les pages ou gérer le panier.
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def loyalty_db():
    # Neon sert uniquement à conserver les points fidélité.
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        return PostgresConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row))
    return db()

def init_db():
    con=db()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT"
    con.execute(f"""CREATE TABLE IF NOT EXISTS orders(
        id {pk},
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
    con.execute(f"""CREATE TABLE IF NOT EXISTS products(
        id {pk},
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
    con.execute(f"""CREATE TABLE IF NOT EXISTS loyalty_events(
        id {pk},
        order_id INTEGER NOT NULL,
        phone TEXT NOT NULL,
        delta INTEGER NOT NULL,
        kind TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(order_id, kind)
    )""")
    count=con.execute("SELECT COUNT(*) c FROM products").fetchone()["c"]
    if count==0:
        for p in DEFAULT_PRODUCTS:
            con.execute("""INSERT INTO products(category,name,description,price,menu_price,image,active)
                           VALUES(?,?,?,?,?,?,1)""",(p["cat"],p["name"],p["desc"],p["price"],p["menu"],p["img"]))
    else:
        # Ajoute automatiquement les nouveaux produits du menu sans toucher aux produits existants.
        existing={(r["category"], r["name"]) for r in con.execute("SELECT category,name FROM products").fetchall()}
        for p in DEFAULT_PRODUCTS:
            if (p["cat"], p["name"]) not in existing:
                con.execute("""INSERT INTO products(category,name,description,price,menu_price,image,active)
                               VALUES(?,?,?,?,?,?,1)""",(p["cat"],p["name"],p["desc"],p["price"],p["menu"],p["img"]))
    defaults={
        "restaurant_name":"HC BURGER FRAIS",
        "address":"24 Boulevard Banon, 13004 Marseille",
        "phone":"04 91 49 38 68",
        "hours_1":"Lun–Jeu & Sam : 11:00 → 00:00",
        "hours_2":"Ven & Dim : 14:00 → 00:00",
        "delivery_postcodes":"13003,13004,13005,13012,13013,13014",
        "delivery_fee":"3",
        "delivery_label":"Livraison 13003/13004/13005/13012/13013/13014",
        "allow_sur_place":"1",
        "allow_takeaway":"1",
        "allow_delivery":"1",
        "allow_pay_restaurant":"1",
        "allow_pay_online":"1",
        "restaurant_open":"1"
    }
    for k,v in defaults.items():
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
    con.commit(); con.close()

    # Initialise seulement la table fidélité sur Neon.
    if DATABASE_URL:
        lcon = loyalty_db()
        lcon.execute("""CREATE TABLE IF NOT EXISTS loyalty_events(
            id BIGSERIAL PRIMARY KEY,
            order_id BIGINT NOT NULL,
            phone TEXT NOT NULL,
            delta INTEGER NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(order_id, kind)
        )""")
        # Les identifiants de fidélité combinent le timestamp et l'id SQLite local
        # (ex. ~1.7e16) afin d'éviter les collisions après un redéploi Render.
        # Une ancienne table Neon pouvait encore avoir order_id en INTEGER 32 bits,
        # ce qui faisait échouer « Accepter » avant la mise à jour du statut.
        lcon.execute("ALTER TABLE loyalty_events ALTER COLUMN order_id TYPE BIGINT")
        lcon.commit(); lcon.close()

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

def normalize_phone(phone):
    return "".join(ch for ch in (phone or "") if ch.isdigit())

def loyalty_event_order_id(created_at, local_order_id):
    """Identifiant stable même si le SQLite local repart à 1 après un redéploi Render."""
    try:
        ms = int(datetime.fromisoformat(str(created_at)).timestamp() * 1000)
    except Exception:
        ms = int(datetime.now(PARIS_TZ).timestamp() * 1000)
    return ms * 10000 + int(local_order_id)

def loyalty_balance(phone, con=None):
    phone = normalize_phone(phone)
    if not phone:
        return 0
    own = con is None
    if own:
        con = loyalty_db()
    row = con.execute("SELECT COALESCE(SUM(delta),0) AS b FROM loyalty_events WHERE phone=?", (phone,)).fetchone()
    if own:
        con.close()
    return max(0, int(row["b"] or 0))

@app.get("/api/loyalty")
def api_loyalty():
    phone = request.args.get("phone", "")
    b = loyalty_balance(phone)
    return jsonify(ok=True, points=b, gift_available=b >= 5)

def ticket_text(o):
    items=json.loads(o["items_json"])
    s=get_settings()
    lines=[s["restaurant_name"],s["address"],s["phone"],"-"*32,
           f"COMMANDE #{o['id']}",o["created_at"],"-"*32]
    for it in items:
        if it.get("loyalty_gift") is True:
            lines.append("*** CADEAU FIDELITE - MENU OFFERT ***")
        elif it.get("formula") == "offert" or it.get("promo") is True:
            lines.append("*** OFFERT ***")
        qty = it.get("qty", it.get("quantity", 1))
        name = it.get("name", "")
        formula = it.get("formula", "")
        if formula in ("menu", "loyalty"):
            detail = f"Menu - {it.get('drink','')}"
        elif formula == "offert":
            detail = "Seul"
        else:
            detail = it.get("kind", "Seul")
        lines.append(f"{name} x {qty}")
        lines.append(detail)
        lines.append("-"*32)
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
    # Une seule connexion DB pour charger l’accueil.
    # Avec Neon, ouvrir 3 connexions successives rendait le retour après “Ajouter au panier” perceptiblement plus lent.
    con = db()
    products = [dict(r) for r in con.execute(
        "SELECT * FROM products WHERE active=1 ORDER BY category,name"
    ).fetchall()]
    settings = {r["key"]: r["value"] for r in con.execute(
        "SELECT key,value FROM settings"
    ).fetchall()}
    con.close()
    restaurant_open = settings.get("restaurant_open", "1") == "1"
    return render_template("index.html", products=products, settings=settings, restaurant_open=restaurant_open)

@app.post("/api/orders")
def create_order():
    if not restaurant_is_open():
        return jsonify(ok=False,error="Restaurant fermé — commandes indisponibles actuellement."),403
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
        if postcode not in DELIVERY_RULES:
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
        fee = delivery_fee_for(postcode, total)
        if fee is None:
            return jsonify(ok=False,error="Livraison non disponible pour ce code postal"),400
        total += fee

    con=db()
    insert_sql = """INSERT INTO orders(created_at,status,customer_name,phone,order_type,address,payment,payment_status,note,total,items_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)"""
    cur=con.execute(insert_sql,
                    (datetime.now(PARIS_TZ).isoformat(),"new",name,phone,typ,
                     (address+" "+postcode).strip(),payment,"unpaid",note,total,json.dumps(clean,ensure_ascii=False)))
    oid = cur.lastrowid
    con.commit()
    o=con.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    printed,msg=try_network_print(o)
    con.close()

    if payment=="Paiement CB en ligne":
        if not STRIPE_SECRET_KEY:
            return jsonify(ok=True,order_id=oid,total=total,printed=printed,print_message=msg,tracking_url=url_for("order_tracking", oid=oid),
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
            return jsonify(ok=True,order_id=oid,total=total,printed=printed,checkout_url=checkout.url,tracking_url=url_for("order_tracking", oid=oid),payment_ready=True)
        except Exception as e:
            return jsonify(ok=True,order_id=oid,total=total,printed=printed,payment_ready=False,tracking_url=url_for("order_tracking", oid=oid),
                           message=f"Commande enregistrée mais paiement non lancé: {e}")
    return jsonify(ok=True,order_id=oid,total=total,printed=printed,print_message=msg,tracking_url=url_for("order_tracking", oid=oid),payment_ready=True)

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

@app.get("/api/admin/restaurant-status")
@admin_required
def admin_restaurant_status():
    return jsonify(open=restaurant_is_open())

@app.post("/api/admin/restaurant-status")
@admin_required
def admin_set_restaurant_status():
    data = request.get_json(silent=True) or {}
    is_open = bool(data.get("open"))
    con = db()
    con.execute("INSERT INTO settings(key,value) VALUES('restaurant_open',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if is_open else "0",))
    con.commit(); con.close()
    return jsonify(ok=True, open=is_open)

@app.post("/api/admin/orders/<int:oid>/status")
@admin_required
def admin_status(oid):
    st=request.json.get("status")
    if st not in ["accepted","rejected","done"]: return jsonify(ok=False),400
    con=db()
    order=con.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
    if not order: con.close(); return jsonify(ok=False),404
    items=json.loads(order["items_json"] or "[]")
    phone=normalize_phone(order["phone"])
    loyalty_order_id = loyalty_event_order_id(order["created_at"], oid)
    # Les points fidélité sont crédités uniquement quand l'admin termine la commande.
    # Chaque Menu payé = 1 point. INSERT OR IGNORE évite tout double comptage.
    if st == "done" and str(order["order_type"] or "").lower() in ("emporter", "sur_place"):
        # Un cadeau fidélité ne rapporte pas de point, mais les Menus payants
        # ajoutés dans la même commande rapportent bien leurs points.
        paid_menus = [it for it in items if it.get("formula") == "menu" and not it.get("promo") and not it.get("loyalty_gift")]
        earned_points = sum(max(1, int(it.get("quantity", 1) or 1)) for it in paid_menus)
        if earned_points > 0:
            lcon = loyalty_db()
            lcon.execute(
                "INSERT OR IGNORE INTO loyalty_events(order_id,phone,delta,kind,created_at) VALUES(?,?,?,?,?)",
                (loyalty_order_id, phone, earned_points, "earned", datetime.now(PARIS_TZ).isoformat())
            )
            lcon.commit(); lcon.close()
    # Si la commande est refusée, on annule les points gagnés ou le cadeau consommé dans Neon.
    if st == "rejected":
        lcon = loyalty_db()
        lcon.execute("DELETE FROM loyalty_events WHERE order_id=?",(loyalty_order_id,))
        lcon.commit(); lcon.close()
    con.execute("UPDATE orders SET status=? WHERE id=?",(st,oid)); con.commit(); con.close()
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
                         f.get("image", ""), 1, int(f["id"])))
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
@app.context_processor
def inject_cart_count():
    cart_data = session.get("cart", {})
    cart_count = sum(int(q) for q in cart_data.values())
    return {"cart_count": cart_count}
def supplements_extra_price(category, supplements):
    """Prix des suppléments selon la catégorie, calculé côté serveur."""
    if not supplements:
        return 0.0
    if category == "Salades":
        prices = {"Œuf": 1.0, "Poulet mariné": 2.5, "Tender": 2.5, "Steak haché": 2.5}
        return sum(prices.get(x, 0.0) for x in supplements)
    if category in ("Burgers", "Tacos"):
        return 1.0 * len(supplements)
    return 0.0

@app.get("/cart")
def cart():
    cart_data = session.get("cart", {})
    customizations = session.get("cart_customizations", {})
    items = []

    con = db()

    for pid, quantity in cart_data.items():
        product = con.execute(
            "SELECT * FROM products WHERE id = ?",
            (int(pid),)
        ).fetchone()

        if product:
            choices = customizations.get(str(pid), [])
            choice = choices[-1] if choices else {}

            supplements = choice.get("supplements", [])
            extra_price = supplements_extra_price(product["category"], supplements)

            items.append({
                "id": product["id"],
                "name": product["name"],
                "price": float(product["price"]) + extra_price,
                "quantity": quantity,
                "viande": choice.get("viande", ""),
                "sauce": choice.get("sauce", ""),
                "supplements": supplements,
                "garnitures": choice.get("garnitures", [])
            })

    con.close()

    total = sum(item["price"] * item["quantity"] for item in items)

    # Ne jamais afficher un cadeau fidélité conservé dans une ancienne session
    # si le client n'a plus réellement 5 points dans Neon.
    pending_loyalty_gift = session.get("loyalty_gift_choice", "").strip()
    loyalty_phone = normalize_phone(session.get("loyalty_phone", ""))
    if pending_loyalty_gift:
        if not loyalty_phone or loyalty_balance(loyalty_phone) < 5:
            session.pop("loyalty_gift_choice", None)
            session.pop("loyalty_phone", None)
            pending_loyalty_gift = ""

    return render_template("cart.html", items=items, total=total, promo_gifts=PROMO_GIFTS,
                           loyalty_gifts=list(LOYALTY_GIFTS.keys()),
                           pending_loyalty_gift=pending_loyalty_gift)

@app.post("/loyalty/gift/add")
def add_loyalty_gift():
    phone = request.form.get("phone", "").strip()
    phone_key = normalize_phone(phone)
    choice = request.form.get("loyalty_gift", "").strip()
    if not phone_key or loyalty_balance(phone_key) < 5:
        session.pop("loyalty_gift_choice", None)
        session.pop("loyalty_phone", None)
        return "Vous n'avez pas encore 5 points fidélité.", 400
    if choice not in LOYALTY_GIFTS:
        return "Cadeau fidélité invalide.", 400
    session["loyalty_gift_choice"] = choice
    session["loyalty_phone"] = phone_key
    return redirect(url_for("cart"))

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
        customizations = session.get("cart_customizations", {})
        choices = customizations.get(key, [])
    if choices:
        choices.pop()
    if choices:
        customizations[key] = choices
    else:
        customizations.pop(key, None)
    session["cart_customizations"] = customizations
    return redirect(url_for("cart"))
@app.get("/cart/add/<int:pid>")
def add_to_cart(pid):
    cart = session.get("cart", {})
    key = str(pid)

    cart[key] = cart.get(key, 0) + 1
    session["cart"] = cart

    viande_map = {
        "1": "Kebab",
        "2": "Viande hachée",
        "3": "Poulet mariné",
        "4": "Escalope",
        "5": "Tenders",
        "6": "Cordon bleu",
        "7": "Nuggets"
    }

    sauce_map = {
        "1": "Algérienne",
        "2": "Harissa",
        "3": "Mayonnaise",
        "4": "Biggy",
        "5": "Barbecue",
        "6": "Andalouse",
        "7": "Samouraï",
        "8": "Brésil",
        "9": "Ketchup"
    }

    supplement_map = {
        # Tacos (existants)
        "1": "Œuf", "2": "Emmental", "3": "Cheddar", "4": "Chèvre",
        "5": "Raclette", "6": "Vache Kiri", "7": "Bacon",
        # Burgers
        "b_oeuf": "Œuf", "b_bacon": "Bacon", "b_raclette": "Raclette",
        "b_chevre": "Chèvre", "b_vache": "La Vache qui rit", "b_kiri": "Kiri",
        "b_emmental": "Emmental", "b_oignons": "Oignons cuits",
        # Salades
        "s_oeuf": "Œuf", "s_poulet": "Poulet mariné",
        "s_tender": "Tender", "s_steak": "Steak haché"
    }
    garniture_map = {
        "1": "Salade",
        "2": "Tomate",
        "3": "Oignon"
    }
    viande_code = request.args.get("viande", "").strip()
    sauce_code = request.args.get("sauce", "").strip()
    supp_codes = request.args.get("supplements", "").split(",")
    garniture_codes = request.args.get("garnitures", "")
    garnitures = [garniture_map[g.strip()] for g in garniture_codes.split(",") if g.strip() in garniture_map]
    viande = ", ".join(viande_map[c.strip()] for c in viande_code.split(",") if c.strip() in viande_map)
    sauce = sauce_map.get(sauce_code, "")
    supplements = [
        supplement_map[c.strip()]
        for c in supp_codes
        if c.strip() in supplement_map
    ]

    if viande or sauce or supplements or garnitures:
        customizations = session.get("cart_customizations", {})
        choices = customizations.get(key, [])

        choices.append({
            "viande": viande,
            "sauce": sauce,
            "supplements": supplements,
            "garnitures": garnitures
        })

        customizations[key] = choices
        session["cart_customizations"] = customizations
    # AJAX add-to-cart: do not follow a redirect back to /, because that
    # needlessly reloads products/settings from the remote database.
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return ("", 204)
    return redirect(url_for("home"))
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
        if not restaurant_is_open():
            return "🔴 Restaurant fermé — commandes indisponibles actuellement.", 403
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
        customizations = session.get("cart_customizations", {})
        choices = customizations.get(str(pid), [])
        choice = choices[-1] if choices else {}
        choice_supplements = choice.get("supplements", [])
        extra_price = supplements_extra_price(product["category"], choice_supplements)
        unit_price += extra_price
        total = unit_price * quantity
        items = [{
            "product_id": pid,
            "name": item_name,
            "quantity": quantity,
            "price": unit_price,
            "viande": choice.get("viande"),
            "sauce": choice.get("sauce"),
            "supplements": choice_supplements,
            "formula": formula,
            "drink": drink
        }]
        cur = con.execute(
                """INSERT INTO orders
                (created_at, status, customer_name, phone, order_type,
                 address, payment, payment_status, note, total, items_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(PARIS_TZ).isoformat(),
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
        oid = cur.lastrowid
        con.commit()
        con.close()
        return redirect(url_for("order_tracking", oid=oid))
    con.close()
    return render_template("order.html", product=product)  
@app.post("/cart/checkout")
def cart_checkout():
    if not restaurant_is_open():
        return "🔴 Restaurant fermé — commandes indisponibles actuellement.", 403  
    cart_data = session.get("cart", {})

    pending_loyalty_gift = session.get("loyalty_gift_choice", "")
    if not cart_data and not pending_loyalty_gift:
        return redirect(url_for("cart"))

    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    order_type = request.form.get("order_type", "emporter")
    address = request.form.get("address", "").strip()
    postal = request.form.get("postal_code", "").strip()
    if order_type == "livraison":
        if postal not in DELIVERY_RULES:
            return "Livraison disponible uniquement dans les codes postaux 13003, 13004, 13005, 13012, 13013 et 13014.", 400
        if not address:
            return "Adresse de livraison requise.", 400
        address = f"{address}, {postal}"
    if not name or not phone:
            return "Nom et téléphone requis", 400

    con = db()
    items = []
    total = 0

    for pid, quantity in cart_data.items():
        product = con.execute(
            "SELECT * FROM products WHERE id = ?",
            (int(pid),)
        ).fetchone()

        if not product:
            continue

        quantity = int(quantity)
        formula = request.form.get(f"formula_{pid}", "seul")
        # Un menu est autorisé uniquement pour les produits qui ont un prix menu.
        # Les Menu Enfant restent à prix fixe (6,90 €).
        if formula == "menu" and product["menu_price"] is None:
            formula = "seul"
        drink = request.form.get(f"drink_{pid}", "")

        unit_price = float(product["price"])
        item_name = product["name"]

        customizations = session.get("cart_customizations", {})
        choices = customizations.get(str(pid), [])
        for i in range(quantity):
            choice = choices[i] if i < len(choices) else {}

            viande = choice.get("viande", "")
            sauce = choice.get("sauce", "")
            supplements = choice.get("supplements", [])
            garnitures = choice.get("garnitures", [])

            item_unit_price = unit_price + supplements_extra_price(product["category"], supplements)

            if formula == "menu":
                # Utilise le vrai prix menu enregistré (ex. Tenders 5→7,50 ; 8→10,50).
                item_unit_price = float(product["menu_price"]) + supplements_extra_price(product["category"], supplements)

            items.append({
                "product_id": int(pid),
                "name": item_name,
                "category": product["category"],
                "quantity": 1,
                "price": item_unit_price,
                "formula": formula,
                "drink": drink if formula == "menu" else "",
                "viande": viande,
                "sauce": sauce,
                "supplements": supplements,
                "garnitures": garnitures
            })

            total += item_unit_price

    # Fidélité : après 5 points, le client choisit son cadeau. Il peut le prendre seul
    # ou ajouter d'autres produits. Valable uniquement Sur place / À emporter.
    loyalty_choice = session.get("loyalty_gift_choice", "").strip()
    loyalty_drink = request.form.get("loyalty_drink", "").strip()
    loyalty_viande = request.form.get("loyalty_viande", "").strip()
    loyalty_sauce = request.form.get("loyalty_sauce", "").strip()
    loyalty_garnitures = [g.strip() for g in request.form.getlist("loyalty_garnitures") if g.strip()]
    phone_key = normalize_phone(phone)
    balance = loyalty_balance(phone_key)

    if loyalty_choice:
        if order_type not in ("emporter", "sur_place"):
            con.close(); return "Le cadeau fidélité est disponible uniquement Sur place ou À emporter.", 400
        if session.get("loyalty_phone") and session.get("loyalty_phone") != phone_key:
            con.close(); return "Utilisez le même numéro de téléphone que celui de votre fidélité.", 400
        if balance < 5:
            con.close(); return "Vous n'avez pas encore 5 points fidélité.", 400
        if loyalty_choice not in LOYALTY_GIFTS:
            con.close(); return "Cadeau fidélité invalide.", 400
        db_name = LOYALTY_GIFTS[loyalty_choice]
        gift_product = con.execute("SELECT * FROM products WHERE name=? AND active=1 LIMIT 1", (db_name,)).fetchone()
        if not gift_product:
            con.close(); return "Cadeau fidélité indisponible.", 400
        is_menu_gift = loyalty_choice.startswith("Menu ")
        is_tacos_gift = loyalty_choice == "Menu Tacos"
        is_sandwich_gift = loyalty_choice.startswith("Menu Sandwich ")
        allowed_viandes = {"Kebab", "Viande hachée", "Poulet mariné", "Escalope", "Tenders", "Cordon bleu", "Nuggets"}
        allowed_sauces = {"Algérienne", "Harissa", "Mayonnaise", "Biggy", "Barbecue", "Andalouse", "Samouraï", "Brésil", "Ketchup"}
        allowed_garnitures = {"Salade", "Tomate", "Oignon"}
        if is_tacos_gift:
            if loyalty_viande not in allowed_viandes or loyalty_sauce not in allowed_sauces:
                con.close(); return "Choisissez la viande et la sauce de votre Tacos offert.", 400
            loyalty_garnitures = []
        elif is_sandwich_gift:
            loyalty_viande = ""
            if loyalty_sauce not in allowed_sauces:
                con.close(); return "Choisissez la sauce de votre Sandwich offert.", 400
            loyalty_garnitures = [g for g in loyalty_garnitures if g in allowed_garnitures]
        else:
            loyalty_viande = ""; loyalty_sauce = ""; loyalty_garnitures = []
        items.append({
            "product_id": int(gift_product["id"]), "name": loyalty_choice, "category": gift_product["category"],
            "quantity": 1, "price": 0.0, "formula": "loyalty",
            "drink": loyalty_drink if is_menu_gift else "",
            "viande": loyalty_viande, "sauce": loyalty_sauce, "supplements": [], "garnitures": loyalty_garnitures, "loyalty_gift": True
        })

    # Promo: 2 menus (ou plus) dans la commande = 1 produit offert au choix.
    # Le contrôle est refait côté serveur pour empêcher un cadeau sans 2 menus.
    menu_count = sum(1 for item in items if item.get("formula") == "menu")
    promo_gift = request.form.get("promo_gift", "").strip()

    if menu_count >= 2:
        if promo_gift not in PROMO_GIFTS:
            con.close()
            return "Choisissez votre produit offert.", 400

        gift_product = con.execute(
            "SELECT * FROM products WHERE name = ? AND active = 1 LIMIT 1",
            (promo_gift,)
        ).fetchone()
        if not gift_product:
            con.close()
            return "Produit offert indisponible.", 400

        # Options du cadeau gratuit (mêmes choix utiles que le produit normal).
        gift_viande = request.form.get("promo_gift_viande", "").strip()
        gift_sauce = request.form.get("promo_gift_sauce", "").strip()
        gift_garnitures = [g.strip() for g in request.form.getlist("promo_gift_garnitures") if g.strip()]

        # On ne garde que les options qui correspondent au cadeau choisi.
        if promo_gift == "Tacos M":
            gift_garnitures = []
        elif promo_gift == "Sandwich Kebab":
            gift_viande = ""
        else:
            gift_viande = ""
            gift_sauce = ""
            gift_garnitures = []

        items.append({
            "product_id": int(gift_product["id"]),
            "name": gift_product["name"],
            "quantity": 1,
            "price": 0.0,
            "formula": "offert",
            "drink": "",
            "viande": gift_viande,
            "sauce": gift_sauce,
            "supplements": [],
            "garnitures": gift_garnitures,
            "promo": True
        })

    # Livraison : frais de 3 € sous le seuil de gratuité de la zone.
    # 13004 : gratuite dès 10 €. 13003/13005 : gratuite dès 15 €. 13012/13013/13014 : gratuite dès 20 €.
    # La fidélité (6e menu offert) ne s'applique pas aux livraisons.
    if order_type == "livraison":
        fee = delivery_fee_for(postal, total)
        if fee is None:
            con.close()
            return "Livraison non disponible pour ce code postal.", 400
        total += fee

    checkout_insert_sql = """INSERT INTO orders
        (created_at, status, customer_name, phone, order_type,
         address, payment, payment_status, note, total, items_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    order_created_at = datetime.now(PARIS_TZ).isoformat()
    cur = con.execute(
        checkout_insert_sql,
        (
            order_created_at,
            "pending",
            name,
            phone,
            order_type,
            address,
            "cash",
            "unpaid",
            "",
            total,
            json.dumps(items)
        )
    )
    oid = cur.lastrowid
    loyalty_order_id = loyalty_event_order_id(order_created_at, oid)

    if loyalty_choice:
        # Le 6e cadeau consomme les 5 points dans Neon uniquement.
        lcon = loyalty_db()
        lcon.execute("INSERT OR IGNORE INTO loyalty_events(order_id,phone,delta,kind,created_at) VALUES(?,?,?,?,?)",
                     (loyalty_order_id,phone_key,-5,"redeemed",datetime.now(PARIS_TZ).isoformat()))
        lcon.commit(); lcon.close()
    # Les points des Menus payés ne sont PAS crédités ici.
    # Ils seront ajoutés seulement lorsque l'admin clique « Terminer ».

    con.commit()
    con.close()

    session["cart"] = {}
    session["cart_customizations"] = {}
    session.pop("loyalty_gift_choice", None)
    session.pop("loyalty_phone", None)
    return redirect(url_for("order_tracking", oid=oid))

@app.get("/commande/<int:oid>")
def order_tracking(oid):
    # Page légère : aucune connexion Neon. Elle ne lit que le statut de la commande locale.
    con = db()
    order = con.execute("SELECT id, status FROM orders WHERE id=?", (oid,)).fetchone()
    con.close()
    if not order:
        return "Commande introuvable", 404
    return render_template("order_tracking.html", order_id=oid, initial_status=order["status"])

@app.get("/api/orders/<int:oid>/status")
def public_order_status(oid):
    # Ne renvoie aucune donnée personnelle : seulement le numéro et le statut.
    con = db()
    order = con.execute("SELECT id, status FROM orders WHERE id=?", (oid,)).fetchone()
    con.close()
    if not order:
        return jsonify(ok=False), 404
    return jsonify(ok=True, order_id=order["id"], status=order["status"])

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)

# Pages légales
@app.route('/mentions-legales')
def mentions_legales():
    return render_template('mentions_legales.html')

@app.route('/confidentialite')
def confidentialite():
    return render_template('confidentialite.html')

@app.route('/cgv')
def cgv():
    return render_template('cgv.html')
