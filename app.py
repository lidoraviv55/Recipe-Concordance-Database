import os
from flask import Flask, render_template, request, redirect, url_for, flash, session
from markupsafe import Markup
import re
from tokenizer import tokenize, parse_recipe_text
import json
import config
from db import get_conn, bootstrap_db

bootstrap_db()
app = Flask(__name__)
import os

app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")


# -------------------------
# Utilities
# -------------------------

def _collect_edit_blocks(segments):
    """
    Utility to split reconstructed segments into blocks for the editor:
    returns (headnote_text, ingredients_lines[], steps_lines[])
    """
    head = ""
    ings, steps = [], []
    for s in segments:
        if s["type"] == "HEADNOTE" and s["index"] == 1:
            head = s["text"]
        elif s["type"] == "INGREDIENT":
            ings.append(s["text"])
        elif s["type"] == "STEP":
            steps.append(s["text"])
    return head, ings, steps


def _delete_recipe_segments(conn, recipe_id):
    with conn.cursor() as cur:
        # FK cascade from recipe_segments→segment_tokens, but we delete tokens first for clarity
        cur.execute("SELECT segment_id FROM recipe_segments WHERE recipe_id=%s", (recipe_id,))
        segs = [r["segment_id"] for r in cur.fetchall()]
        if segs:
            placeholders = ",".join(["%s"] * len(segs))
            cur.execute(f"DELETE FROM segment_tokens WHERE segment_id IN ({placeholders})", tuple(segs))
        cur.execute("DELETE FROM recipe_segments WHERE recipe_id=%s", (recipe_id,))


def _fetch_existing_words(conn, words):
    """
    Return dict {word: word_id} for the given iterable of words.
    Uses IN() batching; words is small per segment, so one round-trip is fine.
    """
    if not words:
        return {}
    placeholders = ",".join(["%s"] * len(words))
    sql = f"SELECT word, word_id FROM words WHERE word IN ({placeholders})"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(words))
        return {row["word"]: row["word_id"] for row in cur.fetchall()}


def _insert_missing_words(conn, missing_words):
    """
    INSERT IGNORE to avoid unique conflicts; then refetch ids.
    """
    if not missing_words:
        return
    with conn.cursor() as cur:
        cur.executemany("INSERT IGNORE INTO words (word) VALUES (%s)", [(w,) for w in missing_words])


def insert_segment(conn, recipe_id, seg_type, seg_index, tokens):
    """
    Bulk insert for a segment and its tokens.
    1) Insert segment row
    2) Upsert words in bulk
    3) Insert tokens with executemany
    """
    token_count = len(tokens)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recipe_segments (recipe_id, segment_type, segment_index, token_count) VALUES (%s,%s,%s,%s)",
            (recipe_id, seg_type, seg_index, token_count),
        )
        segment_id = cur.lastrowid

    # Collect unique words
    unique_words = []
    seen = set()
    for w, _ in tokens:
        if w not in seen:
            seen.add(w)
            unique_words.append(w)

    # Fetch existing, insert missing, then refetch all
    existing = _fetch_existing_words(conn, unique_words)
    missing = [w for w in unique_words if w not in existing]
    _insert_missing_words(conn, missing)
    # merge map
    word_map = existing.copy()
    if missing:
        word_map.update(_fetch_existing_words(conn, missing))

    # Prepare token rows
    rows = []
    for i, (w, joiner) in enumerate(tokens, start=1):
        wid = word_map[w]
        rows.append((segment_id, i, wid, joiner))

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO segment_tokens (segment_id, word_order, word_id, joiner) VALUES (%s,%s,%s,%s)",
            rows
        )
    return segment_id


def slugify(s: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in s).strip("-")


def ensure_word(conn, word):
    """
    Returns word_id from words table, inserting if needed.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT word_id FROM words WHERE word=%s", (word,))
        row = cur.fetchone()
        if row:
            return row["word_id"]
        cur.execute("INSERT INTO words (word) VALUES (%s)", (word,))
        return cur.lastrowid


def insert_recipe(conn, meta):
    """
    Insert into recipes and return recipe_id.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recipes
            (title, section, order_in_section, variant_no, kashrut_type, holiday_flags,
             primary_ingredient, methods, yield_text, servings, index_terms, source_ref)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'Project Gutenberg – The International Jewish Cook Book')
            """,
            (
                meta["title"], meta["section"], meta["order_in_section"], meta["variant_no"],
                meta["kashrut_type"], meta["holiday_flags"], meta["primary_ingredient"],
                meta["methods"], meta["yield_text"], meta["servings"], meta["index_terms"]
            ),
        )
        return cur.lastrowid


def insert_segment(conn, recipe_id, seg_type, seg_index, tokens):
    """
    Insert a segment and all tokens.
    tokens is list of (word, joiner)
    """
    token_count = len(tokens)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recipe_segments (recipe_id, segment_type, segment_index, token_count) VALUES (%s,%s,%s,%s)",
            (recipe_id, seg_type, seg_index, token_count),
        )
        segment_id = cur.lastrowid

        for i, (w, joiner) in enumerate(tokens, start=1):
            word_id = ensure_word(conn, w)
            cur.execute(
                "INSERT INTO segment_tokens (segment_id, word_order, word_id, joiner) VALUES (%s,%s,%s,%s)",
                (segment_id, i, word_id, joiner),
            )

    return segment_id


def reconstruct_segment(conn, segment_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT w.word, IFNULL(t.joiner, '') AS joiner
            FROM segment_tokens t
                     JOIN words w ON w.word_id = t.word_id
            WHERE t.segment_id = %s
            ORDER BY t.word_order
            """,
            (segment_id,),
        )
        parts = cur.fetchall()
        return "".join(p["word"] + p["joiner"] for p in parts)


def reconstruct_recipe(conn, recipe_id):
    """
    Returns dict of segments grouped by type with ordered indices.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT segment_id, segment_type, segment_index
            FROM recipe_segments
            WHERE recipe_id = %s
            ORDER BY FIELD(segment_type, 'TITLE', 'HEADNOTE', 'INGREDIENT', 'STEP', 'NOTE'), segment_index
            """,
            (recipe_id,),
        )
        rows = cur.fetchall()

    grouped = []
    for r in rows:
        text = reconstruct_segment(conn, r["segment_id"])
        grouped.append({
            "segment_id": r["segment_id"],
            "type": r["segment_type"],
            "index": r["segment_index"],
            "text": text,
        })
    return grouped


# -------------------------
# Routes
# -------------------------

@app.route("/")
def index():
    # Show the landing page with filters and a populated "Choose recipe" dropdown.
    with get_conn() as conn, conn.cursor() as cur:
        # how many recipes we have
        cur.execute("SELECT COUNT(*) AS n FROM recipes")
        n_recipes = cur.fetchone()["n"]

        # sections for the Section filter
        cur.execute("SELECT DISTINCT section FROM recipes ORDER BY section")
        sections = [r["section"] for r in cur.fetchall()]

        # kashrut options (distinct, non-empty)
        cur.execute(
            "SELECT DISTINCT kashrut_type FROM recipes WHERE kashrut_type IS NOT NULL AND kashrut_type <> '' ORDER BY kashrut_type")
        kashrut_options = [row["kashrut_type"] for row in cur.fetchall()]

        # method options: explode the SET into unique tokens
        cur.execute("SELECT methods FROM recipes WHERE methods IS NOT NULL AND methods <> ''")
        method_set = set()
        for row in cur.fetchall():
            for token in (row["methods"] or "").split(","):
                token = token.strip()
                if token:
                    method_set.add(token)
        method_options = sorted(method_set)

        # recipes for the positional-search dropdown
        cur.execute("SELECT recipe_id, title FROM recipes ORDER BY title")
        recipes_dd = cur.fetchall()

    return render_template(
        "index.html",
        n_recipes=n_recipes,
        sections=sections,
        recipes_dd=recipes_dd,
        kashrut_options=kashrut_options,
        method_options=method_options,
        pos_result=None,
        q=""
    )


@app.route("/recipes")
def list_recipes():
    section = request.args.get("section", "").strip()
    kashrut = request.args.get("kashrut", "").strip().upper()
    method = request.args.get("method", "").strip().upper()

    sql = "SELECT recipe_id, title, section, kashrut_type, methods, primary_ingredient FROM recipes WHERE 1=1"
    params = []
    if section:
        sql += " AND section=%s"
        params.append(section)
    if kashrut in ("MEAT", "DAIRY", "PARVE", "UNKNOWN"):
        sql += " AND kashrut_type=%s"
        params.append(kashrut)
    if method:
        sql += " AND FIND_IN_SET(%s, methods) > 0"
        params.append(method)

    sql += " ORDER BY section, order_in_section, title"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        return render_template("recipes.html", recipes=rows, section=section, kashrut=kashrut, method=method)


@app.route("/recipe/<int:recipe_id>")
def recipe_detail(recipe_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM recipes WHERE recipe_id=%s", (recipe_id,))
        recipe = cur.fetchone()
        if not recipe:
            flash("Recipe not found", "warning")
            return redirect(url_for("list_recipes"))

        segments = reconstruct_recipe(conn, recipe_id)
        return render_template("recipe_detail.html", recipe=recipe, segments=segments)


@app.route("/upload")
def upload_redirect():
    return redirect(url_for("import_recipe"))


@app.route("/kwic", methods=["GET", "POST"])
def kwic():
    context = {"hits": [], "word": "", "rid": None, "window": 5}
    if request.method == "POST":
        word = (request.form.get("word") or "").strip().lower()
        rid = request.form.get("recipe_id") or None
        window = int(request.form.get("window") or "5")
        context["word"] = word
        context["rid"] = rid
        context["window"] = window

        if word:
            with get_conn() as conn, conn.cursor() as cur:
                # find hits - optionally filter by recipe
                if rid:
                    cur.execute("""
                                SELECT s.segment_id, s.segment_index, t.word_order, r.recipe_id, r.title AS recipe_title
                                FROM recipe_segments s
                                         JOIN segment_tokens t ON t.segment_id = s.segment_id
                                         JOIN words w ON w.word_id = t.word_id
                                         JOIN recipes r ON r.recipe_id = s.recipe_id
                                WHERE s.recipe_id = %s
                                  AND s.segment_type = 'STEP'
                                  AND w.word = %s
                                ORDER BY s.segment_index, t.word_order
                                """, (rid, word))
                else:
                    # Search across ALL recipes
                    cur.execute("""
                                SELECT s.segment_id, s.segment_index, t.word_order, r.recipe_id, r.title AS recipe_title
                                FROM recipe_segments s
                                         JOIN segment_tokens t ON t.segment_id = s.segment_id
                                         JOIN words w ON w.word_id = t.word_id
                                         JOIN recipes r ON r.recipe_id = s.recipe_id
                                WHERE s.segment_type = 'STEP'
                                  AND w.word = %s
                                ORDER BY r.title, s.segment_index, t.word_order
                                """, (word,))
                hits = cur.fetchall()

                windows = []
                for h in hits:
                    # window tokens
                    cur.execute("""
                                SELECT w.word, IFNULL(t.joiner, '') AS joiner
                                FROM segment_tokens t
                                         JOIN words w ON w.word_id = t.word_id
                                WHERE t.segment_id = %s
                                  AND t.word_order BETWEEN %s AND %s
                                ORDER BY t.word_order
                                """, (h["segment_id"], h["word_order"] - window, h["word_order"] + window))
                    toks = cur.fetchall()
                    snippet_raw = "".join(x["word"] + x["joiner"] for x in toks)
                    # Highlight the searched word within the snippet. Use case-insensitive
                    # matching and wrap occurrences with a yellow background. Mark the result
                    # as safe HTML so it won't be auto-escaped by Jinja2.
                    highlighted = snippet_raw
                    if word:
                        pattern = re.compile(r"\b(" + re.escape(word) + r")\b", flags=re.IGNORECASE)
                        highlighted = pattern.sub(lambda m: f"<span class='bg-warning'>" + m.group(0) + "</span>",
                                                  snippet_raw)
                    windows.append({
                        "segment_index": h["segment_index"],
                        "hit_at": h["word_order"],
                        "snippet": Markup(highlighted),
                        "recipe_id": h["recipe_id"],
                        "recipe_title": h["recipe_title"]
                    })
                context["hits"] = windows

    # load recipes for select
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT recipe_id, title FROM recipes ORDER BY title")
        recipes = cur.fetchall()
    return render_template("kwic.html", recipes=recipes, **context)


from pymysql import err as pymysql_err  # add near your imports if not present


@app.route("/groups", methods=["GET", "POST"])
def groups():
    """
    Word Groups: full CRUD
      - Create/Read/Update/Delete groups
      - Create/Read/Update/Delete items
      - Search (KWIC) for all words in a group with filters
    Uses PRG and Bootstrap modals for in-place edits.
    """
    selected_group_id = request.args.get("selected_group_id")  # keep dropdowns preselected after actions
    action = request.form.get("action") if request.method == "POST" else None

    # ---------------------------
    # CREATE GROUP
    # ---------------------------
    if action == "create_group":
        name = (request.form.get("group_name") or "").strip()
        desc = (request.form.get("group_desc") or "").strip()
        if not name:
            flash("Please enter a group name.", "warning")
            return redirect(url_for("groups"))
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO word_groups (group_name, description) VALUES (%s,%s)", (name, desc))
            new_id = cur.lastrowid
        flash("Group created.", "success")
        return redirect(url_for("groups", selected_group_id=new_id))

    # ---------------------------
    # UPDATE GROUP (name/desc)
    # ---------------------------
    if action == "update_group":
        gid = request.form.get("edit_group_id")
        name = (request.form.get("edit_group_name") or "").strip()
        desc = (request.form.get("edit_group_desc") or "").strip()
        if gid and name:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("UPDATE word_groups SET group_name=%s, description=%s WHERE group_id=%s",
                            (name, desc, gid))
            flash("Group updated.", "success")
            return redirect(url_for("groups", selected_group_id=gid))
        flash("Missing group or name.", "warning")
        return redirect(url_for("groups"))

    # ---------------------------
    # DELETE GROUP (and its items)
    # ---------------------------
    if action == "delete_group":
        gid = request.form.get("del_group_id")
        if gid:
            with get_conn() as conn, conn.cursor() as cur:
                # If FK CASCADE exists, the next line is enough:
                # cur.execute("DELETE FROM word_groups WHERE group_id=%s", (gid,))
                # For safety across schemas, delete items first:
                cur.execute("DELETE FROM word_group_items WHERE group_id=%s", (gid,))
                cur.execute("DELETE FROM word_groups WHERE group_id=%s", (gid,))
            flash("Group deleted.", "success")
        else:
            flash("Group not found.", "warning")
        return redirect(url_for("groups"))

    # ---------------------------
    # ADD ITEM
    # ---------------------------
    if action == "add_item":
        group_id = request.form.get("group_id")
        word = (request.form.get("word") or "").strip().lower()
        scope = request.form.get("scope") or "STEP"
        # Always treat items as global: ignore any provided recipe_id
        recipe_id = None
        if word and group_id:
            with get_conn() as conn:
                word_id = ensure_word(conn, word)
                with conn.cursor() as cur:
                    cur.execute("""
                                INSERT INTO word_group_items (group_id, word_id, recipe_id, scope)
                                VALUES (%s, %s, %s, %s) ON DUPLICATE KEY
                                UPDATE scope=
                                VALUES (scope)
                                """, (group_id, word_id, recipe_id, scope))
            flash("Word added to group.", "success")
            return redirect(url_for("groups", selected_group_id=group_id))
        flash("Please choose a group and enter a word.", "warning")
        return redirect(url_for("groups"))

    # ---------------------------
    # UPDATE ITEM
    # ---------------------------
    if action == "update_item":
        item_id = request.form.get("edit_item_id")
        group_id = request.form.get("edit_item_group_id")
        word = (request.form.get("edit_item_word") or "").strip().lower()
        scope = request.form.get("edit_item_scope") or "STEP"
        # Ignore recipe_id for updates; items are global
        recipe_id = None
        if not item_id or not group_id or not word:
            flash("Missing item/group/word.", "warning")
            return redirect(url_for("groups", selected_group_id=group_id or ""))

        with get_conn() as conn, conn.cursor() as cur:
            wid = ensure_word(conn, word)
            try:
                cur.execute("""
                            UPDATE word_group_items
                            SET word_id=%s,
                                recipe_id=%s,
                                scope=%s
                            WHERE item_id = %s
                            """, (wid, recipe_id, scope, item_id))
                flash("Item updated.", "success")
            except pymysql_err.IntegrityError as e:
                # Unique conflict → a same (group,word,recipe,scope) already exists → merge by deleting old item
                if getattr(e, "args", []) and e.args[0] == 1062:
                    cur.execute("DELETE FROM word_group_items WHERE item_id=%s", (item_id,))
                    flash("Item merged into existing entry.", "success")
                else:
                    raise
        return redirect(url_for("groups", selected_group_id=group_id))

    # ---------------------------
    # DELETE ITEM
    # ---------------------------
    if action == "delete_item":
        item_id = request.form.get("del_item_id")
        group_id = request.form.get("del_item_group_id")
        if item_id:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM word_group_items WHERE item_id=%s", (item_id,))
            flash("Item deleted.", "success")
        else:
            flash("Item not found.", "warning")
        return redirect(url_for("groups", selected_group_id=group_id or ""))

    # ---------------------------
    # SEARCH GROUP (show recipes containing any word in group)
    # ---------------------------
    group_hits = []
    if action == "search_group":
        sel_group_id = request.form.get("sel_group_id")
        sel_recipe_id = request.form.get("sel_recipe_id") or None
        sel_scope = request.form.get("sel_scope") or ""  # TITLE/INGREDIENT/STEP or empty = all

        selected_group_id = sel_group_id or selected_group_id

        if sel_group_id:
            with get_conn() as conn, conn.cursor() as cur:
                joins = []
                wheres = ["g.group_id=%s"]
                params = [sel_group_id]

                joins.append("JOIN word_group_items gi ON gi.group_id = g.group_id")
                joins.append("JOIN words w           ON w.word_id = gi.word_id")
                joins.append("JOIN segment_tokens t  ON t.word_id = w.word_id")
                # Handle GLOBAL scope: match all segment types, otherwise match specific scope
                joins.append("JOIN recipe_segments s ON s.segment_id = t.segment_id AND (gi.scope = 'GLOBAL' OR s.segment_type = gi.scope)")
                joins.append("JOIN recipes r         ON r.recipe_id = s.recipe_id")

                if sel_recipe_id:
                    wheres.append("s.recipe_id = %s")
                    params.append(sel_recipe_id)
                if sel_scope in ("TITLE", "INGREDIENT", "STEP"):
                    wheres.append("s.segment_type = %s")
                    params.append(sel_scope)

                # Get distinct recipes that contain any word from the group
                sql = " ".join([
                    "SELECT DISTINCT r.recipe_id, r.title",
                    "FROM word_groups g",
                    *joins,
                    "WHERE " + " AND ".join(wheres),
                    "ORDER BY r.title"
                ])
                cur.execute(sql, tuple(params))
                group_hits = cur.fetchall()
        else:
            flash("Please choose a group to search.", "warning")

    # ---------------------------
    # LOAD lists (for GET and after actions)
    # ---------------------------
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT group_id, group_name, description FROM word_groups ORDER BY group_name")
        groups_list = cur.fetchall()

        group_items = {}
        for g in groups_list:
            cur.execute("""
                        SELECT gi.item_id, gi.group_id, w.word, w.word_id, gi.scope
                        FROM word_group_items gi
                                 JOIN words w ON w.word_id = gi.word_id
                        WHERE gi.group_id = %s
                          AND gi.recipe_id IS NULL
                        ORDER BY w.word
                        """, (g["group_id"],))
            items = cur.fetchall()

            # For each item, find recipes where this word appears in the specified scope
            for item in items:
                if item["scope"] == "GLOBAL":
                    # GLOBAL scope: search in all segment types
                    cur.execute("""
                                SELECT DISTINCT r.recipe_id, r.title
                                FROM segment_tokens t
                                         JOIN recipe_segments s ON s.segment_id = t.segment_id
                                         JOIN recipes r ON r.recipe_id = s.recipe_id
                                WHERE t.word_id = %s
                                  AND s.segment_type IN ('TITLE', 'INGREDIENT', 'STEP')
                                ORDER BY r.title
                                """, (item["word_id"],))
                else:
                    # Specific scope: search only in that segment type
                    cur.execute("""
                                SELECT DISTINCT r.recipe_id, r.title
                                FROM segment_tokens t
                                         JOIN recipe_segments s ON s.segment_id = t.segment_id
                                         JOIN recipes r ON r.recipe_id = s.recipe_id
                                WHERE t.word_id = %s
                                  AND s.segment_type = %s
                                ORDER BY r.title
                                """, (item["word_id"], item["scope"]))
                item["recipes"] = cur.fetchall()
                item["recipe_count"] = len(item["recipes"])

            group_items[g["group_id"]] = items

        cur.execute("SELECT recipe_id, title FROM recipes ORDER BY title")
        recipes = cur.fetchall()

    return render_template(
        "groups.html",
        groups_list=groups_list,
        group_items=group_items,
        recipes=recipes,
        group_hits=group_hits,
        selected_group_id=selected_group_id
    )


@app.route("/expressions", methods=["GET", "POST"])
def expressions():
    results = []

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        # ---------- CREATE EXPRESSION ----------
        if action == "create_expression":
            name = (request.form.get("expr_name") or "").strip()
            phrase = (request.form.get("expr_phrase") or "").strip().lower()
            try:
                max_gap = int(request.form.get("max_gap") or "0")
            except ValueError:
                max_gap = 0

            if not name:
                flash("Please enter an expression name.", "warning")
            elif not phrase:
                flash("Please enter a phrase.", "warning")
            else:
                words = [w for w in phrase.split() if w]
                if not words:
                    flash("Please enter at least one word in the phrase.", "warning")
                else:
                    try:
                        with get_conn() as conn, conn.cursor() as cur:
                            # Check if this exact combination already exists
                            cur.execute(
                                "SELECT expression_id FROM expressions WHERE name=%s AND max_gap=%s",
                                (name, max_gap)
                            )
                            if cur.fetchone():
                                flash(f"Expression '{name}' with gap={max_gap} already exists. Please choose a different name or gap value.", "warning")
                            else:
                                cur.execute(
                                    "INSERT INTO expressions (name, description, is_order_sensitive, max_gap) "
                                    "VALUES (%s, '', 1, %s)",
                                    (name, max_gap)
                                )
                                expr_id = cur.lastrowid

                                pos = 1
                                for w in words:
                                    # ensure_word is defined earlier in app.py – we can reuse it here
                                    w_id = ensure_word(conn, w)
                                    cur.execute(
                                        "INSERT INTO expression_terms (expression_id, position_index, word_id) "
                                        "VALUES (%s, %s, %s)",
                                        (expr_id, pos, w_id)
                                    )
                                    pos += 1

                                flash("Expression created.", "success")
                                return redirect(url_for("expressions"))
                    except pymysql_err.IntegrityError as e:
                        # This should only happen if the unique constraint (name, max_gap) is violated
                        error_code = getattr(e, "args", [None])[0] if getattr(e, "args", None) else None
                        if error_code == 1062:
                            flash(f"Expression '{name}' with gap={max_gap} already exists. Please choose a different name or gap value.", "warning")
                        else:
                            flash(f"Database error creating expression: {e}", "warning")
                    except Exception as e:
                        flash(f"Error creating expression: {e}", "warning")

        # ---------- SEARCH EXPRESSION ----------
        elif action == "search_expression":
            expr_search = (request.form.get("expr_search_name") or "").strip()
            
            # Parse name and gap from format "name|gap"
            if "|" in expr_search:
                expr_name, expr_gap_str = expr_search.split("|", 1)
                try:
                    expr_gap = int(expr_gap_str)
                except ValueError:
                    expr_gap = None
            else:
                expr_name = expr_search
                expr_gap = None

            with get_conn() as conn, conn.cursor() as cur:
                if expr_gap is not None:
                    cur.execute(
                        "SELECT expression_id, max_gap FROM expressions WHERE name=%s AND max_gap=%s",
                        (expr_name, expr_gap)
                    )
                else:
                    cur.execute(
                        "SELECT expression_id, max_gap FROM expressions WHERE name=%s LIMIT 1",
                        (expr_name,)
                    )
                expr = cur.fetchone()
                if not expr:
                    flash("Expression not found.", "warning")
                else:
                    cur.execute(
                        "SELECT position_index, word_id "
                        "FROM expression_terms "
                        "WHERE expression_id=%s "
                        "ORDER BY position_index",
                        (expr["expression_id"],)
                    )
                    terms = cur.fetchall()
                    if not terms:
                        flash("Expression has no terms.", "warning")
                    else:
                        # Build: SELECT ... FROM ... JOIN ... WHERE ... (JOINs before WHERE!)
                        selects = [
                            "SELECT DISTINCT s.segment_id, s.recipe_id, r.title AS recipe_title, s.segment_type, s.segment_index"
                        ]
                        froms = ["FROM recipe_segments s"]
                        joins = [
                            "JOIN recipes r ON r.recipe_id = s.recipe_id",
                            "JOIN segment_tokens t1 ON t1.segment_id = s.segment_id"
                        ]
                        wheres = [
                            "s.segment_type IN ('TITLE','INGREDIENT','STEP')",
                            "t1.word_id = %s"
                        ]
                        params = [terms[0]["word_id"]]

                        prev_alias = "t1"
                        for i, t in enumerate(terms[1:], start=2):
                            alias = f"t{i}"
                            if expr["max_gap"] == 0:
                                joins.append(
                                    f"JOIN segment_tokens {alias} "
                                    f"ON {alias}.segment_id = s.segment_id "
                                    f"AND {alias}.word_order = {prev_alias}.word_order + 1"
                                )
                                wheres.append(f"{alias}.word_id = %s")
                                params.append(t["word_id"])
                            else:
                                joins.append(
                                    f"JOIN segment_tokens {alias} "
                                    f"ON {alias}.segment_id = s.segment_id "
                                    f"AND {alias}.word_order BETWEEN {prev_alias}.word_order + 1 "
                                    f"AND {prev_alias}.word_order + 1 + %s"
                                )
                                params.append(expr["max_gap"])
                                wheres.append(f"{alias}.word_id = %s")
                                params.append(t["word_id"])
                            prev_alias = alias

                        sql = " ".join(
                            selects + froms + joins + ["WHERE " + " AND ".join(wheres),
                                                       "ORDER BY r.title, s.segment_type, s.segment_index"]
                        )
                        cur.execute(sql, tuple(params))
                        raw_results = cur.fetchall()
                        
                        # Reconstruct segment text for each result
                        results = []
                        for row in raw_results:
                            segment_text = reconstruct_segment(conn, row["segment_id"])
                            results.append({
                                "recipe_id": row["recipe_id"],
                                "recipe_title": row["recipe_title"],
                                "segment_type": row["segment_type"],
                                "segment_index": row["segment_index"],
                                "segment_text": segment_text
                            })

    # Load expression list for the page
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT expression_id, name, max_gap FROM expressions ORDER BY name")
        expressions_list = cur.fetchall()

    # Get action and selected expression for template
    action = (request.form.get("action") or "").strip() if request.method == "POST" else ""
    selected_expr = request.form.get("expr_search_name") or request.args.get("expr_search_name") or ""
    
    # Get expression words for highlighting (if search was performed)
    expression_words = []
    if action == "search_expression" and results:
        # Extract words from the selected expression
        if "|" in selected_expr:
            expr_name, _ = selected_expr.split("|", 1)
        else:
            expr_name = selected_expr
        
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT expression_id FROM expressions WHERE name=%s LIMIT 1", (expr_name,))
            expr = cur.fetchone()
            if expr:
                cur.execute("""
                    SELECT w.word 
                    FROM expression_terms et
                    JOIN words w ON w.word_id = et.word_id
                    WHERE et.expression_id = %s
                    ORDER BY et.position_index
                """, (expr["expression_id"],))
                expression_words = [row["word"] for row in cur.fetchall()]
    
    return render_template("expressions.html", expressions=expressions_list, results=results, 
                          action=action, selected_expr=selected_expr, expression_words=expression_words)


@app.route("/stats", methods=["GET", "POST"])
def stats():
    # Get filter parameters
    filter_digits = request.form.get("filter_digits") or request.args.get("filter_digits") or "all"  # "all", "digits_only", "exclude_digits"
    filter_group_id = request.form.get("filter_group_id") or request.args.get("filter_group_id")
    
    with get_conn() as conn, conn.cursor() as cur:
        # Build filter conditions for word-based queries
        word_filter_joins = []
        word_filter_wheres = []
        word_filter_params = []
        
        if filter_group_id:
            # Filter by word group
            word_filter_joins.append("JOIN word_group_items gi ON gi.word_id = w.word_id")
            word_filter_wheres.append("gi.group_id = %s AND gi.recipe_id IS NULL")
            word_filter_params.append(filter_group_id)
        
        if filter_digits == "digits_only":
            # Filter to only words containing digits
            word_filter_wheres.append("w.word REGEXP '[0-9]'")
        elif filter_digits == "exclude_digits":
            # Filter to exclude words containing digits
            word_filter_wheres.append("w.word NOT REGEXP '[0-9]'")
        
        word_filter_join_sql = " ".join(word_filter_joins)
        word_filter_where_sql = (" AND " + " AND ".join(word_filter_wheres)) if word_filter_wheres else ""
        
        # Count recipes (filtered by group if specified)
        if filter_group_id:
            cur.execute("""
                SELECT COUNT(DISTINCT r.recipe_id) AS n
                FROM recipes r
                JOIN recipe_segments s ON s.recipe_id = r.recipe_id
                JOIN segment_tokens t ON t.segment_id = s.segment_id
                JOIN words w ON w.word_id = t.word_id
                JOIN word_group_items gi ON gi.word_id = w.word_id
                WHERE gi.group_id = %s AND gi.recipe_id IS NULL
            """, (filter_group_id,))
            n_recipes = cur.fetchone()["n"]
        else:
            cur.execute("SELECT COUNT(*) AS n FROM recipes")
            n_recipes = cur.fetchone()["n"]

        # Count segments (filtered by group if specified)
        if filter_group_id:
            cur.execute("""
                SELECT COUNT(DISTINCT s.segment_id) AS n
                FROM recipe_segments s
                JOIN segment_tokens t ON t.segment_id = s.segment_id
                JOIN words w ON w.word_id = t.word_id
                JOIN word_group_items gi ON gi.word_id = w.word_id
                WHERE gi.group_id = %s AND gi.recipe_id IS NULL
            """, (filter_group_id,))
            n_segments = cur.fetchone()["n"]
        else:
            cur.execute("SELECT COUNT(*) AS n FROM recipe_segments")
            n_segments = cur.fetchone()["n"]

        # Count tokens (with filters)
        token_count_sql = f"""
            SELECT COUNT(*) AS n
            FROM segment_tokens t
            JOIN words w ON w.word_id = t.word_id
            {word_filter_join_sql}
            WHERE 1=1 {word_filter_where_sql}
        """
        cur.execute(token_count_sql, tuple(word_filter_params))
        n_tokens = cur.fetchone()["n"]

        # top words in steps (with filters)
        try:
            top_words_sql = f"""
                SELECT w.word, COUNT(*) AS c
                FROM segment_tokens t
                JOIN words w ON w.word_id = t.word_id
                JOIN recipe_segments s ON s.segment_id = t.segment_id
                {word_filter_join_sql}
                WHERE s.segment_type = 'STEP'
                  AND w.is_stop = 0
                  {word_filter_where_sql}
                GROUP BY w.word
                ORDER BY c DESC LIMIT 20
            """
            cur.execute(top_words_sql, tuple(word_filter_params))
            top_words = cur.fetchall()
        except:
            # Fallback if is_stop column doesn't exist
            top_words_sql = f"""
                SELECT w.word, COUNT(*) AS c
                FROM segment_tokens t
                JOIN words w ON w.word_id = t.word_id
                JOIN recipe_segments s ON s.segment_id = t.segment_id
                {word_filter_join_sql}
                WHERE s.segment_type = 'STEP'
                  {word_filter_where_sql}
                GROUP BY w.word
                ORDER BY c DESC LIMIT 20
            """
            cur.execute(top_words_sql, tuple(word_filter_params))
            top_words = cur.fetchall()

        # distribution by section (filtered by group if specified)
        if filter_group_id:
            cur.execute("""
                SELECT r.section, COUNT(DISTINCT r.recipe_id) AS c
                FROM recipes r
                JOIN recipe_segments s ON s.recipe_id = r.recipe_id
                JOIN segment_tokens t ON t.segment_id = s.segment_id
                JOIN words w ON w.word_id = t.word_id
                JOIN word_group_items gi ON gi.word_id = w.word_id
                WHERE gi.group_id = %s AND gi.recipe_id IS NULL
                GROUP BY r.section
                ORDER BY c DESC
            """, (filter_group_id,))
            by_section = cur.fetchall()
        else:
            cur.execute("""
                SELECT section, COUNT(*) AS c
                FROM recipes
                GROUP BY section
                ORDER BY c DESC
            """)
            by_section = cur.fetchall()

        # distribution by kashrut (filtered by group if specified)
        if filter_group_id:
            cur.execute("""
                SELECT r.kashrut_type, COUNT(DISTINCT r.recipe_id) AS c
                FROM recipes r
                JOIN recipe_segments s ON s.recipe_id = r.recipe_id
                JOIN segment_tokens t ON t.segment_id = s.segment_id
                JOIN words w ON w.word_id = t.word_id
                JOIN word_group_items gi ON gi.word_id = w.word_id
                WHERE gi.group_id = %s AND gi.recipe_id IS NULL
                GROUP BY r.kashrut_type
            """, (filter_group_id,))
            by_kashrut = cur.fetchall()
        else:
            cur.execute("""
                SELECT kashrut_type, COUNT(*) AS c
                FROM recipes
                GROUP BY kashrut_type
            """)
            by_kashrut = cur.fetchall()

        # Load word groups for filter dropdown
        cur.execute("SELECT group_id, group_name FROM word_groups ORDER BY group_name")
        word_groups = cur.fetchall()

    return render_template("stats.html",
                           n_recipes=n_recipes, n_segments=n_segments, n_tokens=n_tokens,
                           top_words=top_words, by_section=by_section, by_kashrut=by_kashrut,
                           word_groups=word_groups, filter_digits=filter_digits, 
                           filter_group_id=filter_group_id)


@app.route("/search", methods=["GET", "POST"])
def search():
    """
    Main search page:
      A) Text search (single word or multi-word phrase) with filters:
         - section, kashrut, method
      B) Positional search:
         - find the word by (recipe, segment type, line number, word number) and show KWIC + full line.

    Expects _positional_lookup(conn, recipe_id, segment_type, line_number, word_number, window)
    to be already defined elsewhere in app.py (as you mentioned).
    """
    # Text-search inputs (filters + query)
    section = request.form.get("section", "").strip() if request.method == "POST" else ""
    kashrut = request.form.get("kashrut", "").strip().upper() if request.method == "POST" else ""
    method = request.form.get("method", "").strip().upper() if request.method == "POST" else ""
    q = (request.form.get("q") or "").strip().lower() if request.method == "POST" else ""

    # Positional-search inputs
    pos_result = None
    if request.method == "POST" and request.form.get("pos_action") == "pos_lookup":
        # Gather positional fields; validate gently
        try:
            pos_recipe_id = int(request.form.get("pos_recipe_id") or "0")
            pos_seg_type = (request.form.get("pos_segment_type") or "").strip().upper()
            pos_line_number = int(request.form.get("pos_line_number") or "0")
            pos_word_number = int(request.form.get("pos_word_number") or "0")
            pos_window = int(request.form.get("pos_window") or "5")
        except ValueError:
            pos_recipe_id = 0
            pos_seg_type = ""
            pos_line_number = 0
            pos_word_number = 0
            pos_window = 5

        if pos_recipe_id > 0 and pos_seg_type in ("TITLE", "INGREDIENT",
                                                  "STEP") and pos_line_number > 0 and pos_word_number > 0:
            with get_conn() as conn:
                pos_result = _positional_lookup(conn,
                                                pos_recipe_id,
                                                pos_seg_type,
                                                pos_line_number,
                                                pos_word_number,
                                                pos_window)
        else:
            pos_result = {
                "error": "Please fill recipe, segment type (TITLE/INGREDIENT/STEP), line number (>0) and word number (>0)."}

    # Text search results (if not doing a positional lookup)
    results = None  # None means no search performed yet
    if request.method == "POST" and request.form.get("pos_action") != "pos_lookup":
        with get_conn() as conn, conn.cursor() as cur:
            # Build common filter SQL for recipes (section/kashrut/method)
            filter_sql = "WHERE 1=1"
            params = []
            if section:
                filter_sql += " AND r.section=%s"
                params.append(section)
            if kashrut in ("MEAT", "DAIRY", "PARVE", "UNKNOWN"):
                filter_sql += " AND r.kashrut_type=%s"
                params.append(kashrut)
            if method:
                filter_sql += " AND FIND_IN_SET(%s, r.methods) > 0"
                params.append(method)

            if not q:
                # Empty q → just list recipes under current filters
                cur.execute(f"""
                    SELECT r.recipe_id, r.title, r.section
                    FROM recipes r
                    {filter_sql}
                    ORDER BY r.title
                    LIMIT 200
                """, tuple(params))
                results = cur.fetchall()
            else:
                parts = [p for p in q.split() if p]
                if len(parts) == 1:
                    # Single word: lookup in dictionary and then tokens
                    cur.execute("SELECT word_id FROM words WHERE word=%s", (parts[0],))
                    wrow = cur.fetchone()
                    if not wrow:
                        results = []
                    else:
                        wid = wrow["word_id"]
                        cur.execute(f"""
                            SELECT DISTINCT r.recipe_id, r.title, r.section
                            FROM segment_tokens t
                            JOIN recipe_segments s ON s.segment_id = t.segment_id
                            JOIN recipes r ON r.recipe_id = s.recipe_id
                            {filter_sql} AND t.word_id=%s
                            ORDER BY r.title
                            LIMIT 200
                        """, tuple(params + [wid]))
                        results = cur.fetchall()
                else:
                    # Multi-word phrase: adjacency-based positional match
                    # 1) Resolve all word_ids
                    wids = []
                    for w in parts:
                        cur.execute("SELECT word_id FROM words WHERE word=%s", (w,))
                        d = cur.fetchone()
                        if not d:
                            wids = []
                            break
                        wids.append(d["word_id"])
                    if not wids:
                        results = []
                    else:
                        # 2) Build joins for adjacency: t1, t2, ..., tn
                        n = len(wids)
                        base = f"""
                            SELECT DISTINCT r.recipe_id, r.title, r.section
                            FROM recipes r
                            JOIN recipe_segments s ON s.recipe_id = r.recipe_id
                            JOIN segment_tokens t1 ON t1.segment_id = s.segment_id AND t1.word_id=%s
                            {filter_sql}
                        """
                        params2 = [wids[0]] + params
                        prev_alias = "t1"
                        for i in range(2, n + 1):
                            alias = f"t{i}"
                            base += f" JOIN segment_tokens {alias} ON {alias}.segment_id = s.segment_id AND {alias}.word_order = {prev_alias}.word_order + 1 AND {alias}.word_id=%s"
                            params2.append(wids[i - 1])
                            prev_alias = alias
                        base += " ORDER BY r.title LIMIT 200"
                        cur.execute(base, tuple(params2))
                        results = cur.fetchall()
    else:
        # GET request - no search performed, don't show results section
        results = None

    # For the section dropdown and dynamic Kashrut/Method options
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT section FROM recipes ORDER BY section")
        sections = [r["section"] for r in cur.fetchall()]

        cur.execute("SELECT recipe_id, title FROM recipes ORDER BY title")
        recipes_dd = cur.fetchall()

        cur.execute(
            "SELECT DISTINCT kashrut_type FROM recipes WHERE kashrut_type IS NOT NULL AND kashrut_type <> '' ORDER BY kashrut_type")
        kashrut_options = [row["kashrut_type"] for row in cur.fetchall()]

        cur.execute("SELECT methods FROM recipes WHERE methods IS NOT NULL AND methods <> ''")
        method_set = set()
        for row in cur.fetchall():
            for token in (row["methods"] or "").split(","):
                token = token.strip()
                if token:
                    method_set.add(token)
        method_options = sorted(method_set)

    return render_template(
        "index.html",
        results=results, q=q,
        sections=sections, section=section, kashrut=kashrut, method=method,
        pos_result=pos_result, recipes_dd=recipes_dd,
        kashrut_options=kashrut_options, method_options=method_options
    )


def _positional_lookup(conn, recipe_id, segment_type, line_number, word_number, window):
    """
    Find the word by (recipe_id, segment_type, line_number, word_number),
    and return the word, a KWIC snippet (±window tokens), and the full segment text.
    Also returns recipe title and the exact coordinates found.
    """
    with conn.cursor() as cur:
        # 1) find the segment id by (recipe, type, index)
        cur.execute("""
                    SELECT s.segment_id, r.title
                    FROM recipe_segments s
                             JOIN recipes r ON r.recipe_id = s.recipe_id
                    WHERE s.recipe_id = %s
                      AND s.segment_type = %s
                      AND s.segment_index = %s
                    """, (recipe_id, segment_type, line_number))
        seg = cur.fetchone()
        if not seg:
            return {"error": "Segment not found for given recipe/type/line."}

        segment_id = seg["segment_id"]
        recipe_title = seg["title"]

        # 2) find the exact token at word_order
        cur.execute("""
                    SELECT t.word_id, w.word, t.word_order
                    FROM segment_tokens t
                             JOIN words w ON w.word_id = t.word_id
                    WHERE t.segment_id = %s
                      AND t.word_order = %s
                    """, (segment_id, word_number))
        tok = cur.fetchone()
        if not tok:
            return {"error": "Word order not found in that segment."}

        needle = tok["word"]

        # 3) KWIC snippet around that position
        cur.execute("""
                    SELECT w2.word, IFNULL(t2.joiner, '') AS joiner
                    FROM segment_tokens t2
                             JOIN words w2 ON w2.word_id = t2.word_id
                    WHERE t2.segment_id = %s
                      AND t2.word_order BETWEEN %s AND %s
                    ORDER BY t2.word_order
                    """, (segment_id, word_number - window, word_number + window))
        toks = cur.fetchall()
        snippet = "".join(x["word"] + x["joiner"] for x in toks)

        # 4) full segment text (the whole line)
        full_text = reconstruct_segment(conn, segment_id)

        return {
            "recipe_id": recipe_id,
            "recipe_title": recipe_title,
            "segment_type": segment_type,
            "line_number": line_number,
            "word_number": word_number,
            "word": needle,
            "window": window,
            "snippet": snippet,
            "full_text": full_text
        }


@app.route("/word-index", methods=["GET", "POST"])
def word_index():
    """
    Word Index with:
      - Pagination for "Top terms" and "Sample positions"
      - Optional server-rendered contexts (for non-JS fallback)
      - Filters: recipe, group, scope
    Note: AJAX (below) will hit /word-index/context for in-place updates.
    """
    # Determine action and filters
    action = request.form.get("action") if request.method == "POST" else None
    rid = request.values.get("recipe_id")  # accepts GET or POST
    gid = request.values.get("group_id")
    scope = request.values.get("scope") or ""

    # Context window (tokens ± around hit)
    try:
        ctx_window = int(request.values.get("ctx_window") or "5")
    except ValueError:
        ctx_window = 5

    # Pagination controls
    # "Top terms" page and per-page
    try:
        tpage = int(request.values.get("tpage") or "1")
    except ValueError:
        tpage = 1
    T_PER_PAGE = 25

    # "Sample positions" page and per-page
    try:
        ppage = int(request.values.get("ppage") or "1")
    except ValueError:
        ppage = 1
    P_PER_PAGE = 25

    with get_conn() as conn, conn.cursor() as cur:
        # Filters select lists
        cur.execute("SELECT recipe_id, title FROM recipes ORDER BY title")
        recipes = cur.fetchall()
        cur.execute("SELECT group_id, group_name FROM word_groups ORDER BY group_name")
        groups = cur.fetchall()

        # Build WHERE/JOINs from filters
        where_parts = []
        params = []
        joins = []

        if gid:
            joins.append("JOIN word_group_items gi ON gi.word_id = t.word_id AND gi.group_id = %s")
            params.append(gid)
        if rid:
            where_parts.append("s.recipe_id = %s")
            params.append(rid)
        if scope in ("TITLE", "INGREDIENT", "STEP"):
            where_parts.append("s.segment_type = %s")
            params.append(scope)

        where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        joins_sql = (" " + " ".join(joins)) if joins else ""

        # ---------- COUNTS for pagination ----------
        # Count distinct words under filter (for top terms)
        count_terms_sql = " ".join([
            "SELECT COUNT(*) AS cnt FROM (",
            "  SELECT w.word",
            "  FROM segment_tokens t",
            "  JOIN words w ON w.word_id = t.word_id",
            "  JOIN recipe_segments s ON s.segment_id = t.segment_id",
            joins_sql,
            where_sql,
            "  GROUP BY w.word",
            ") x"
        ])
        cur.execute(count_terms_sql, tuple(params))
        total_terms = cur.fetchone()["cnt"]
        total_terms_pages = max(1, (total_terms + T_PER_PAGE - 1) // T_PER_PAGE)
        tpage = max(1, min(tpage, total_terms_pages))
        terms_offset = (tpage - 1) * T_PER_PAGE

        # Count positions under filter (for sample positions)
        count_pos_sql = " ".join([
            "SELECT COUNT(*) AS cnt",
            "FROM segment_tokens t",
            "JOIN words w ON w.word_id = t.word_id",
            "JOIN recipe_segments s ON s.segment_id = t.segment_id",
            "JOIN recipes r ON r.recipe_id = s.recipe_id",
            joins_sql,
            where_sql
        ])
        cur.execute(count_pos_sql, tuple(params))
        total_positions = cur.fetchone()["cnt"]
        total_positions_pages = max(1, (total_positions + P_PER_PAGE - 1) // P_PER_PAGE)
        ppage = max(1, min(ppage, total_positions_pages))
        positions_offset = (ppage - 1) * P_PER_PAGE

        # ---------- PAGINATED: Top terms ----------
        terms_sql = " ".join([
            "SELECT w.word, COUNT(*) AS freq",
            "FROM segment_tokens t",
            "JOIN words w ON w.word_id = t.word_id",
            "JOIN recipe_segments s ON s.segment_id = t.segment_id",
            joins_sql,
            where_sql,
            "GROUP BY w.word",
            "ORDER BY freq DESC",
            "LIMIT %s OFFSET %s"
        ])
        cur.execute(terms_sql, tuple(params + [T_PER_PAGE, terms_offset]))
        agg = cur.fetchall()

        # ---------- PAGINATED: Sample positions ----------
        pos_sql = " ".join([
            "SELECT r.title, s.segment_type, s.segment_index, w.word, t.word_order",
            "FROM segment_tokens t",
            "JOIN words w ON w.word_id = t.word_id",
            "JOIN recipe_segments s ON s.segment_id = t.segment_id",
            "JOIN recipes r ON r.recipe_id = s.recipe_id",
            joins_sql,
            where_sql,
            "ORDER BY r.title, s.segment_type, s.segment_index, t.word_order",
            "LIMIT %s OFFSET %s"
        ])
        cur.execute(pos_sql, tuple(params + [P_PER_PAGE, positions_offset]))
        detail = cur.fetchall()

        # ---------- Optional server-side context (fallback) ----------
        contexts = []
        needle = None
        if action == "show_context":
            # Use same utility as the AJAX endpoint (below)
            # Reuse function to avoid code duplication (or inline here)
            needle = (request.form.get("sel_word") or "").strip().lower()
            contexts = _build_contexts(conn, rid, gid, scope, needle, ctx_window, where_parts, params, joins_sql)

    # Pass pagination meta to template
    pager_terms = {
        "page": tpage, "per": T_PER_PAGE, "total": total_terms, "pages": total_terms_pages
    }
    pager_positions = {
        "page": ppage, "per": P_PER_PAGE, "total": total_positions, "pages": total_positions_pages
    }

    return render_template("word_index.html",
                           recipes=recipes, groups=groups,
                           agg=agg, detail=detail,
                           rid=rid, gid=gid, scope=scope,
                           contexts=contexts, needle=needle, ctx_window=ctx_window,
                           pager_terms=pager_terms, pager_positions=pager_positions)


def _build_contexts(conn, rid, gid, scope, needle, ctx_window, where_parts, params, joins_sql):
    """
    Builds contexts for a selected word under current filters:
      - KWIC snippet (±ctx_window tokens)
      - Full segment text
      - Previous/next line for INGREDIENT/STEP
    """
    contexts = []
    if not needle:
        return contexts

    with conn.cursor() as cur:
        cur.execute("SELECT word_id FROM words WHERE word=%s", (needle,))
        wr = cur.fetchone()
        if not wr:
            return contexts
        wid = wr["word_id"]

        extra_join = ""
        extra_params = []
        if gid:
            extra_join = "JOIN word_group_items gi2 ON gi2.word_id = t.word_id AND gi2.group_id = %s"
            extra_params.append(gid)

        sql_hits = " ".join([
            "SELECT r.recipe_id, r.title, s.segment_id, s.segment_type, s.segment_index, t.word_order",
            "FROM segment_tokens t",
            "JOIN recipe_segments s ON s.segment_id = t.segment_id",
            "JOIN recipes r ON r.recipe_id = s.recipe_id",
            extra_join,
            joins_sql,
            "WHERE t.word_id=%s",
            ("AND " + " AND ".join(where_parts)) if where_parts else "",
            "ORDER BY r.title, s.segment_type, s.segment_index, t.word_order"
        ])
        cur.execute(sql_hits, tuple(extra_params + [wid] + params))
        hits = cur.fetchall()

        for h in hits:
            # KWIC window
            cur.execute("""
                        SELECT w2.word, IFNULL(t2.joiner, '') AS joiner
                        FROM segment_tokens t2
                                 JOIN words w2 ON w2.word_id = t2.word_id
                        WHERE t2.segment_id = %s
                          AND t2.word_order BETWEEN %s AND %s
                        ORDER BY t2.word_order
                        """, (h["segment_id"], h["word_order"] - ctx_window, h["word_order"] + ctx_window))
            toks = cur.fetchall()
            snippet = "".join(x["word"] + x["joiner"] for x in toks)

            # Previous/next segment for INGREDIENT/STEP
            prev_text = next_text = ""
            if h["segment_type"] in ("INGREDIENT", "STEP"):
                # previous
                cur.execute(
                    "SELECT segment_id FROM recipe_segments WHERE recipe_id=%s AND segment_type=%s AND segment_index=%s",
                    (h["recipe_id"], h["segment_type"], h["segment_index"] - 1)
                )
                rprev = cur.fetchone()
                if rprev:
                    prev_text = reconstruct_segment(conn, rprev["segment_id"])

                # next
                cur.execute(
                    "SELECT segment_id FROM recipe_segments WHERE recipe_id=%s AND segment_type=%s AND segment_index=%s",
                    (h["recipe_id"], h["segment_type"], h["segment_index"] + 1)
                )
                rnext = cur.fetchone()
                if rnext:
                    next_text = reconstruct_segment(conn, rnext["segment_id"])

            # main segment text
            main_text = reconstruct_segment(conn, h["segment_id"])

            contexts.append({
                "recipe_id": h["recipe_id"],
                "title": h["title"],
                "segment_type": h["segment_type"],
                "segment_index": h["segment_index"],
                "snippet": snippet,
                "main_text": main_text,
                "prev_text": prev_text,
                "next_text": next_text
            })
    return contexts


@app.route("/word-index/context", methods=["POST"])
def word_index_context_partial():
    """
    AJAX endpoint:
      - Receives filters + sel_word + ctx_window
      - Returns the rendered contexts HTML partial
    """
    rid = request.form.get("recipe_id")
    gid = request.form.get("group_id")
    scope = request.form.get("scope") or ""
    needle = (request.form.get("sel_word") or "").strip().lower()
    try:
        ctx_window = int(request.form.get("ctx_window") or "5")
    except ValueError:
        ctx_window = 5

    with get_conn() as conn, conn.cursor() as cur:
        where_parts = []
        params = []
        joins = []

        if gid:
            joins.append("JOIN word_group_items gi ON gi.word_id = t.word_id AND gi.group_id = %s")
            params.append(gid)
        if rid:
            where_parts.append("s.recipe_id = %s")
            params.append(rid)
        if scope in ("TITLE", "INGREDIENT", "STEP"):
            where_parts.append("s.segment_type = %s")
            params.append(scope)

        joins_sql = (" " + " ".join(joins)) if joins else ""
        contexts = _build_contexts(conn, rid, gid, scope, needle, ctx_window, where_parts, params, joins_sql)

    html = render_template("partials/contexts.html",
                           contexts=contexts, needle=needle, ctx_window=ctx_window)
    return html  # return pure HTML fragment


@app.route("/import", methods=["GET", "POST"])
def import_recipe():
    """
    Step 1: show form (GET)
    Step 2: parse and preview (POST). We DO NOT write to DB here.
    """
    if request.method == "POST":
        # Prefer file; if none, use pasted text
        uploaded = request.files.get("recipe_file")
        pasted = (request.form.get("recipe_text") or "").strip()

        try:
            if uploaded and uploaded.filename:
                if not uploaded.filename.lower().endswith(".txt"):
                    raise ValueError("Please upload a .txt file.")
                raw = uploaded.read().decode("utf-8", errors="replace")
            elif pasted:
                raw = pasted
            else:
                raise ValueError("Please upload a .txt file or paste recipe text.")

            # Parse but DO NOT write to DB
            meta = parse_recipe_text(raw)

            # Remove default kashrut/methods when not provided by the user.
            # If the source text does not explicitly specify "KASHRUT_TYPE:" or "METHODS:",
            # avoid assuming defaults like 'PARVE' or 'BAKED'.
            lower_raw = raw.lower()
            # Clean kashrut_type
            if "kashrut_type:" not in lower_raw:
                meta["kashrut_type"] = ""
            else:
                # normalize to allowed values
                kt = meta.get("kashrut_type", "")
                valid_k = {"MEAT", "DAIRY", "PARVE", "UNKNOWN", "EMPTY"}
                meta["kashrut_type"] = kt.upper() if kt and kt.upper() in valid_k else kt
            # Clean methods
            if "methods:" not in lower_raw:
                meta["methods"] = ""
            else:
                mt = meta.get("methods", "")
                if isinstance(mt, list):
                    mt = ",".join(mt)
                tokens = [t.strip().upper() for t in str(mt).split(",") if t.strip()]
                allowed_methods = {"BAKED", "BOILED", "BROILED", "FRIED", "SAUTEED", "SCALLOPED", "STEAMED", "STEWED",
                                   "ROASTED", "GRILLED", "RAW", "PICKLED", "NULL"}
                meta["methods"] = ",".join([t for t in tokens if t in allowed_methods])

            # Pack a safe preview payload (JSON) for commit step
            preview_payload = json.dumps(meta, ensure_ascii=False)

            return render_template(
                "import_preview.html",
                ok=True,
                meta=meta,
                preview_payload=preview_payload
            )

        except Exception as e:
            # Show the error in the preview page without committing
            return render_template(
                "import_preview.html",
                ok=False,
                error=str(e)
            )

    # GET: show the upload/paste form
    return render_template("import.html")


@app.route("/import/commit", methods=["POST"])
def import_commit():
    """
    Step 3: commit the previewed recipe into DB.
    We read the JSON payload we produced in the preview step.
    """
    payload = request.form.get("preview_payload", "")
    if not payload:
        flash("No preview payload found. Please import again.", "warning")
        return redirect(url_for("import_recipe"))

    try:
        meta = json.loads(payload)
        if not isinstance(meta, dict) or not meta.get("title"):
            raise ValueError("Invalid preview payload.")

        with get_conn() as conn:
            rid = insert_recipe(conn, meta)
            for seg in meta["segments"]:
                tokens = tokenize(seg["text"])
                insert_segment(conn, rid, seg["type"], seg["index"], tokens)

        flash("Recipe imported successfully.", "success")
        return redirect(url_for("recipe_detail", recipe_id=rid))

    except Exception as e:
        flash(f"Commit failed: {e}", "warning")
        return redirect(url_for("import_recipe"))


@app.route("/recipe/<int:recipe_id>/edit", methods=["GET", "POST"])
def recipe_edit(recipe_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM recipes WHERE recipe_id=%s", (recipe_id,))
        recipe = cur.fetchone()
        if not recipe:
            flash("Recipe not found.", "warning")
            return redirect(url_for("list_recipes"))

        if request.method == "POST":
            # --- collect metadata ---
            title = (request.form.get("title") or "").strip()
            section = (request.form.get("section") or "").strip() or "UNSPECIFIED"
            try:
                order_in_section = int(request.form.get("order_in_section") or "0")
            except ValueError:
                order_in_section = 0
            variant_no = request.form.get("variant_no")
            try:
                variant_no = int(variant_no) if variant_no else None
            except ValueError:
                variant_no = None
            kashrut = (request.form.get("kashrut_type") or "UNKNOWN").upper()
            if kashrut not in ("MEAT", "DAIRY", "PARVE", "UNKNOWN"): kashrut = "UNKNOWN"
            holiday = (request.form.get("holiday_flags") or "").strip()
            primary = (request.form.get("primary_ingredient") or "").strip()
            methods = (request.form.get("methods") or "").strip()  # comma-separated OK for SET
            yield_tx = (request.form.get("yield_text") or "").strip()
            servings = request.form.get("servings")
            try:
                servings = int(servings) if servings else None
            except ValueError:
                servings = None
            index_terms = (request.form.get("index_terms") or "").strip()

            # --- collect text blocks ---
            headnote = (request.form.get("headnote") or "").strip()
            ingredients = [ln.strip() for ln in (request.form.get("ingredients") or "").splitlines() if ln.strip()]
            steps = [ln.strip() for ln in (request.form.get("steps") or "").splitlines() if ln.strip()]

            # Update metadata
            cur.execute("""
                        UPDATE recipes
                        SET title=%s,
                            section=%s,
                            order_in_section=%s,
                            variant_no=%s,
                            kashrut_type=%s,
                            holiday_flags=%s,
                            primary_ingredient=%s,
                            methods=%s,
                            yield_text=%s,
                            servings=%s,
                            index_terms=%s,
                            updated_at=NOW()
                        WHERE recipe_id = %s
                        """, (title, section, order_in_section, variant_no, kashrut, holiday, primary,
                              methods, yield_tx, servings, index_terms, recipe_id))

            # Rebuild segments/tokens
            _delete_recipe_segments(conn, recipe_id)

            # Title segment (always index 1)
            tokens = tokenize(title) if title else []
            if tokens:
                insert_segment(conn, recipe_id, "TITLE", 1, tokens)

            # Headnote (optional)
            if headnote:
                insert_segment(conn, recipe_id, "HEADNOTE", 1, tokenize(headnote))

            # Ingredients (one per line)
            for i, ln in enumerate(ingredients, start=1):
                insert_segment(conn, recipe_id, "INGREDIENT", i, tokenize(ln))

            # Steps (one per line)
            for i, ln in enumerate(steps, start=1):
                insert_segment(conn, recipe_id, "STEP", i, tokenize(ln))

            flash("Recipe updated.", "success")
            return redirect(url_for("recipe_detail", recipe_id=recipe_id))

        # GET: populate editor
        segments = reconstruct_recipe(conn, recipe_id)
        head, ings, stps = _collect_edit_blocks(segments)
        return render_template("recipe_edit.html", recipe=recipe,
                               headnote=head,
                               ingredients="\n".join(ings),
                               steps="\n".join(stps))


@app.route("/recipe/<int:recipe_id>/delete", methods=["POST"])
def recipe_delete(recipe_id):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT recipe_id FROM recipes WHERE recipe_id=%s", (recipe_id,))
        if not cur.fetchone():
            flash("Recipe not found.", "warning")
            return redirect(url_for("list_recipes"))

        # CASCADE removes segments/tokens and recipe-scoped group items
        cur.execute("DELETE FROM recipes WHERE recipe_id=%s", (recipe_id,))
    flash("Recipe deleted.", "success")
    return redirect(url_for("list_recipes"))


@app.route("/mining", methods=["GET", "POST"])
def mining():
    """
    Frequent co-occurring pairs within the same segment and within a token window.
    This implements the 'data mining' special topic using position-based tokens.
    """
    scope = request.form.get("scope") if request.method == "POST" else "STEP"  # TITLE/INGREDIENT/STEP
    rid = request.form.get("recipe_id") if request.method == "POST" else None
    section = (request.form.get("section") or "").strip() if request.method == "POST" else ""
    try:
        window = int(request.form.get("window") or "5")
    except ValueError:
        window = 5
    try:
        min_support = int(request.form.get("min_support") or "2")
    except ValueError:
        min_support = 2
    exclude_stop = True if (request.form.get("exclude_stop") or "").lower() in ("1", "on", "true", "yes") else False

    with get_conn() as conn, conn.cursor() as cur:
        # For filters UI
        cur.execute("SELECT recipe_id, title FROM recipes ORDER BY title")
        recipes = cur.fetchall()
        cur.execute("SELECT DISTINCT section FROM recipes ORDER BY section")
        sections = [r["section"] for r in cur.fetchall()]

        # ---- Build WHERE filters ----
        where_seg = []
        params_seg = []
        if scope in ("TITLE", "INGREDIENT", "STEP"):
            where_seg.append("s.segment_type = %s")
            params_seg.append(scope)
        if rid:
            where_seg.append("s.recipe_id = %s")
            params_seg.append(rid)
        if section:
            where_seg.append("r.section = %s")
            params_seg.append(section)

        where_seg_sql = ("WHERE " + " AND ".join(where_seg)) if where_seg else ""

        stop_left = "AND w1.is_stop=0" if exclude_stop else ""
        stop_right = "AND w2.is_stop=0" if exclude_stop else ""

        # ---- Pairs with support ----
        # Count pairs w1->w2 appearing in the same segment with distance <= window
        sql_pairs = f"""
            SELECT p.w1, p.w2, p.cnt AS support
            FROM (
                SELECT w1.word AS w1, w2.word AS w2, COUNT(*) AS cnt
                FROM segment_tokens t1
                JOIN segment_tokens t2
                  ON t2.segment_id = t1.segment_id
                 AND t2.word_order > t1.word_order
                 AND t2.word_order <= t1.word_order + %s
                JOIN words w1 ON w1.word_id = t1.word_id {stop_left}
                JOIN words w2 ON w2.word_id = t2.word_id {stop_right}
                JOIN recipe_segments s ON s.segment_id = t1.segment_id
                JOIN recipes r ON r.recipe_id = s.recipe_id
                {where_seg_sql}
                GROUP BY w1.word, w2.word
            ) p
            WHERE p.cnt >= %s
            ORDER BY p.cnt DESC
            LIMIT 200
        """
        params_pairs = [window] + params_seg + [min_support]
        cur.execute(sql_pairs, tuple(params_pairs))
        pairs = cur.fetchall()

        # ---- Left/right word frequencies (to compute confidence) ----
        sql_left = f"""
            SELECT w.word AS w, COUNT(*) AS cnt
            FROM segment_tokens t
            JOIN words w ON w.word_id = t.word_id {"AND w.is_stop=0" if exclude_stop else ""}
            JOIN recipe_segments s ON s.segment_id = t.segment_id
            JOIN recipes r ON r.recipe_id = s.recipe_id
            {where_seg_sql}
            GROUP BY w.word
        """
        cur.execute(sql_left, tuple(params_seg))
        left_freq = {row["w"]: row["cnt"] for row in cur.fetchall()}

        sql_right = sql_left  # symmetric – same set
        cur.execute(sql_right, tuple(params_seg))
        right_freq = {row["w"]: row["cnt"] for row in cur.fetchall()}

    # Compute confidence values in Python for clarity
    enriched = []
    for p in pairs:
        w1, w2, cnt = p["w1"], p["w2"], p["support"]
        lf = left_freq.get(w1, 1)
        rf = right_freq.get(w2, 1)
        conf_w1_to_w2 = round(cnt / lf, 3)
        conf_w2_to_w1 = round(cnt / rf, 3)
        enriched.append({
            "w1": w1, "w2": w2, "support": cnt,
            "conf_w1_to_w2": conf_w1_to_w2,
            "conf_w2_to_w1": conf_w2_to_w1
        })

    return render_template("mining.html",
                           recipes=recipes, sections=sections,
                           pairs=enriched, scope=scope, rid=rid, section=section,
                           window=window, min_support=min_support, exclude_stop=exclude_stop)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)