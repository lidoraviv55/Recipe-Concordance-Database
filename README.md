# Recipe Concordance Database

**Recipe Concordance** converts recipe text (unstructured input) into a **normalized relational schema** in MySQL, enabling **context- and position-based search** (KWIC), phrase/sequence queries, word statistics, and analysis features.

**Repository:**  
https://github.com/lidoraviv55/Recipe-Concordance-Database.git

---

## Tech Stack
- Python 3
- Flask
- MySQL (InnoDB)
- PyMySQL
- python-dotenv

---

## Project Structure
- `app.py` – Flask routes (controller), basic input validation, app entry point  
- `tokenizer.py` – parsing + tokenization (segments + tokens)  
- `db.py` – data access layer (SQL, batch inserts, queries, bootstrap/init)  
- `config.py` – configuration (DB connection, app settings via environment)  
- `templates/`, `static/` – UI layer  
- `requirements.txt` – dependencies

---

## Features
- **Structured hierarchy:** `Recipe → Segments → Tokens`  
- **Context-aware search:** segments preserve meaning (title vs ingredients vs steps)  
- **Normalized dictionary:** `words` table with `UNIQUE(word)` and `word_id` references  
- **KWIC (Key Word In Context):** window-by-position using `word_order`  
- **Expressions:** multi-word expressions with `is_order_sensitive` and `max_gap`  
- **Word Groups:** group words with optional context (`scope`, `recipe_id`)  
- **Data Integrity:** Foreign Keys + **ON DELETE CASCADE** (prevents orphaned data)  
- **Performance:** `executemany` batch inserts + index strategy (validated via `EXPLAIN`)

---

## Setup & Run (Windows)

### Prerequisites
1. Python 3 (recommended 3.10+)
2. MySQL Server running locally (default port `3306`). You can use XAMPP or a local MySQL installation.

### 1) Clone the repository
```powershell
git clone https://github.com/lidoraviv55/Recipe-Concordance-Database.git
cd Recipe-Concordance-Database
```

### 2) Install dependencies

Quick install:
```powershell
pip install -r requirements.txt
```

Recommended (virtual environment):
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3) Configure environment (`.env`)
Create a local `.env` file in the project root (do not commit this file). Example contents:

```
DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
DB_NAME=recipes_db
SECRET_KEY=dev-secret-key-change-me
```

You can also add a `.env.example` to the repo with the same keys and placeholder values.

### 4) Run the application
```powershell
python app.py
```

Open in your browser:
```
http://127.0.0.1:5000/
```

---

## Database & Bootstrapping
- `db.py` contains `bootstrap_db()` which will:
	- create the database (`DB_NAME`) if the configured MySQL user has `CREATE DATABASE` rights, and
	- create all tables (idempotent).
- If the MySQL user lacks privileges, create the database manually and grant appropriate rights:
```sql
CREATE DATABASE recipes_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_520_ci;
GRANT ALL PRIVILEGES ON recipes_db.* TO 'your_user'@'localhost' IDENTIFIED BY 'your_password';
FLUSH PRIVILEGES;
```
- Schema includes: `recipes`, `recipe_segments`, `segment_tokens`, `words`, `word_groups`, `word_group_items`, `expressions`, `expression_terms`.

---

## Sample Data & Import
- Example recipes are included in the `מתכונים לדוגמה/` folder.
- Use the web UI `/import` to upload a `.txt` file or paste recipe text → preview → commit to insert into the DB.

---

## Security Notes
- Uses parameterized SQL queries (prevents SQL injection).  
- Keep secrets out of the repo: do not commit `.env`, DB dumps, or credentials. `.gitignore` contains `.env` by default.  
- Use a strong `SECRET_KEY` for production and a least-privilege DB user (avoid `root` in production).  
- Disable Flask `debug` in production.

---

## Common Issues & Troubleshooting
- MySQL not running: start MySQL (XAMPP or service) and verify port `3306`.  
- Access denied: check `DB_USER`/`DB_PASSWORD` in `.env` and grant proper DB permissions.  
- Port 5000 in use: stop the conflicting process or change Flask port with `PORT` env var.  
- If schema is not created due to permission errors, create the DB manually (see Database section).

---

## Database Schema (ERD summary)
Tables: `recipes`, `recipe_segments`, `segment_tokens`, `words`, `word_groups`, `word_group_items`, `expressions`, `expression_terms`.  
Design goals: normalization, token-position indexing, referential integrity, and efficient search over token positions.

---

## Minimal Commands Summary
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
# create .env (local)
python app.py
```

---

## License & Contact
Include license info here (e.g., MIT) and a short contact line or email for demo/questions.

---

<!-- Removed: prompt to add .env.example/CONTRIBUTING.md -->
