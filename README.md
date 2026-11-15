# FlatFair

## Group number

PP22 — IS2108 Pair Project

## Group members

| Full name | Student number | Email | Contact number | Leader |
|---|---|---|---|---|
| Kwang Le Ping Sharyl | A0334780B | e1668500@u.nus.edu | 98361105 | [ ] |
| Yong Man Ting | A0334762B | e1668482@u.nus.edu | 83339559 | [x] |

## Run instructions

Website: https://flatfair.pengu.app/

Use Python 3.12. Open a terminal in the folder containing `requirements.txt`.

**Windows PowerShell**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cd app/flatfairproj
```

**Linux/macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cd app/flatfairproj
```

For a fresh copy, create `.env` beside `manage.py` using the following example. For an existing local copy, keep its existing secret key.

```dotenv
DEBUG=true
SECRET_KEY=put-your-own-local-key-here
PUBLIC_URL=http://127.0.0.1:8765
FLATFAIR_GPU=off
```

```bash
python manage.py migrate
python manage.py seed_demo
python manage.py seed_activity
python manage.py runserver 127.0.0.1:8765
```

`migrate` creates or updates the database. The two seed commands add demo data. Existing accounts keep their passwords.

Existing servers may already use port 8765. Reuse one or stop it before starting another.

Open **http://127.0.0.1:8765/**.

### Demo logins

| Email | Password |
|---|---|
| buyer@flatfair.sg | buyer12345678 |
| seller@flatfair.sg | seller12345678 |
| admin@flatfair.sg | admin12345678 |

Ctrl+C stops the server.

## YouTube link

https://youtu.be/cl9CnGyYYeU
