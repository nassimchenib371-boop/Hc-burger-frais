HC BURGER FRAIS — VERSION COMPLETE V4

Ce qui est prêt:
- site client
- menu dynamique depuis base de données
- panier
- Sur place / À emporter / Livraison
- zone livraison configurable (13004 par défaut)
- frais livraison configurables
- espace restaurant protégé par mot de passe
- commandes enregistrées en SQLite
- alerte sonore
- accepter / refuser / terminer
- ticket 80mm
- tentative d'impression réseau RAW/ESC-POS vers l'AURES
- gestion produits/prix/activation depuis l'admin
- réglages adresse/téléphone/horaires/zones
- paiement CB via Stripe prévu et activable avec clés

DEMARRAGE LOCAL
Windows PowerShell:
  pip install -r requirements.txt
  $env:ADMIN_PASSWORD="VOTRE_MOT_DE_PASSE"
  python app.py

Mac/Linux:
  pip install -r requirements.txt
  ADMIN_PASSWORD="VOTRE_MOT_DE_PASSE" python3 app.py

Puis:
  Site: http://localhost:8000
  Admin: http://localhost:8000/admin

POUR L'IMPRIMANTE AURES ODP 333
1. Brancher l'imprimante en Ethernet au même réseau.
2. Trouver son adresse IP.
3. Tester si elle accepte RAW/ESC-POS sur port 9100.
4. Lancer avec:
   PRINTER_IP="192.168.1.50" PRINTER_PORT="9100" python3 app.py
   (adapter l'IP)

POUR LE PAIEMENT CB
Créer/avoir un compte marchand Stripe, puis:
  STRIPE_SECRET_KEY="sk_live_..." PUBLIC_BASE_URL="https://votre-domaine.fr" python3 app.py

AVANT MISE EN LIGNE
Il manque forcément les éléments externes qu'un fichier seul ne peut pas créer:
- un hébergement / domaine HTTPS
- le compte de paiement marchand et ses clés
- l'adresse IP/configuration réelle de l'imprimante
- les mentions légales, CGV et politique de confidentialité finalisées avec les informations juridiques de l'entreprise
- test réel d'une commande et d'une impression sur place

SECURITE
Changez ADMIN_PASSWORD et SECRET_KEY avant publication.
