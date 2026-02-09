# Configurare Servicii Externe

Acest document detaliază pașii necesari pentru configurarea serviciilor Twilio, SendGrid și Email (IMAP) pentru a funcționa corect cu aplicația Auto Daune.

## 1. Twilio (WhatsApp)

Aplicația folosește Twilio pentru a primi și trimite mesaje pe WhatsApp.

### A. Obținerea Credențialelor
1.  Loghează-te în consola [Twilio](https://console.twilio.com/).
2.  Copiază **Account SID** și **Auth Token** din dashboard.
3.  Adaugă-le în fișierul `.env` de pe server:
    ```ini
    TWILIO_ACCOUNT_SID=...
    TWILIO_AUTH_TOKEN=...
    ```

### B. Configurare Număr (Sender)
1.  Navighează la **Messaging > Try it out > Send a WhatsApp message** (pentru Sandbox) SAU **Messaging > Senders > WhatsApp Senders** (pentru Producție).
2.  Dacă folosești Sandbox, numărul va fi ceva de genul `+14155238886`.
3.  Dacă ai un număr aprobat (Business Profile), folosește acel număr.
4.  Actualizează `.env`:
    ```ini
    TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886
    ```
    *Notă: Nu uita prefixul `whatsapp:`.*

### C. Configurare Webhook
Pentru ca aplicația să primească mesaje, Twilio trebuie să știe unde să le trimită.

1.  În consola Twilio, mergi la setările numărului de WhatsApp (Sandbox Settings sau Sender Settings).
2.  Găsește câmpul **"When a message comes in"**.
3.  Setează URL-ul către aplicația ta:
    ```
    https://<DOMENIUL-TAU.COM>/bot/webhook/
    ```
4.  Asigură-te că metoda este setată pe **POST**.
5.  Salvează modificările.

### D. Notă despre "24-hour window"
WhatsApp permite boților să răspundă liber doar în primele 24 de ore de la ultimul mesaj al utilizatorului.
*   **În Sandbox**: Această regulă este mai relaxată.
*   **În Producție**: Dacă vrei să inițiezi o conversație (să trimiți un mesaj după 24h fără ca utilizatorul să fi scris primul), trebuie să folosești **Templates** aprobate de WhatsApp. Aplicația curentă trimite mesaje text standard (`send_text`). Dacă întâmpini erori de livrare ("Template required"), va trebui să înregistrezi template-uri în Twilio și să adaptezi codul (`WhatsAppClient`) să le folosească.

---

## 2. SendGrid (Trimitere Email)

Folosim SendGrid ca serviciu SMTP pentru a trimite notificări către asiguratori și confirmări către clienți.

### A. Configurare API Key
1.  Loghează-te în [SendGrid](https://app.sendgrid.com/).
2.  Mergi la **Settings > API Keys**.
3.  Creează o cheie nouă cu acces "Full Access" (sau cel puțin "Mail Send").
4.  Copiază cheia (începe cu `SG...`).

### B. Verificare Sender (Sender Identity)
1.  Mergi la **Settings > Sender Authentication**.
2.  Verifică adresa de email de pe care vei trimite (ex: `office@autodaune.ro`) sau întregul domeniu (`autodaune.ro`).
3.  Fără această verificare, email-urile vor ajunge în SPAM.

### C. Configurare `.env` (SMTP)
În fișierul `.env` de pe server:

```ini
EMAIL_HOST=smtp.sendgrid.net
EMAIL_PORT=587
EMAIL_HOST_USER=apikey
# Da, userul este fix stringul "apikey", nu adresa ta de email!
EMAIL_HOST_PASSWORD=<CHEIA-TA-SG-...>
DEFAULT_FROM_EMAIL=office@autodaune.ro
```

---

## 3. Configurare Primire Email (IMAP)

Aplicația monitorizează inbox-ul (`office@autodaune.ro`) pentru a detecta răspunsurile asiguratorilor. SendGrid **NU** oferă serviciu de stocare email (IMAP), ci doar de trimitere.

Trebuie să folosești setările furnizorului tău de hosting/email (ex: Ionos, Gmail, Zoho, cPanel).

### A. Obținere Date IMAP (Exemplu: Ionos)

Dacă folosești un VPS Ionos cu serviciul de email inclus, setările sunt standard:

*   **IMAP Host**: `imap.ionos.com` (valabil și pentru .de, .co.uk, etc.)
*   **Port**: `993` (SSL/TLS)
*   **Username**: Adresa completă de email (ex: `office@autodaune.ro`)
*   **Password**: Parola setată pentru acea căsuță de email.

⚠️ **Important:** Parola de email poate fi diferită de parola contului de client Ionos. Verifică în *Email & Office > Email settings* din panoul Ionos.

Poți testa conexiunea de pe serverul VPS rulând:
```bash
openssl s_client -crlf -connect imap.ionos.com:993
```
Dacă primești `* OK IMAP4 ready`, serverul răspunde corect.

### B. Configurare `.env` (IMAP)
Deoarece folosim SendGrid pentru trimitere, credențialele SMTP diferă de cele IMAP.
Adaugă aceste variabile dedicate în `.env`:

```ini
# --- Configurare IMAP (Primire - Ionos) ---
IMAP_HOST=imap.ionos.com
IMAP_USER=office@autodaune.ro
IMAP_PASSWORD=<parola-reala-a-contului-email>
```

Aplicația va folosi prioritar `IMAP_USER`/`IMAP_PASSWORD` pentru a se conecta la inbox, ignorând `apikey`-ul setat la `EMAIL_HOST_USER`.
