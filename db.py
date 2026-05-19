import pymysql
from contextlib import contextmanager
import config

# -----------------------------
# Connections
# -----------------------------
def _connect():
    # Normal connection (to a DB). Will work once DB exists.
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,         # requires DB to exist
        autocommit=False,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
    )

def _connect_server():
    # Server-level connection (no database) for CREATE DATABASE IF NOT EXISTS
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        autocommit=True,                 # handy for DDL at server level
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
    )

@contextmanager
def get_conn():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        conn.close()

# -----------------------------
# Bootstrap 
# -----------------------------
DDL = [
    # 1) recipes
    """
    CREATE TABLE IF NOT EXISTS recipes (
      recipe_id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      title                VARCHAR(512) NOT NULL,
      section              VARCHAR(120) NOT NULL,
      order_in_section     INT NOT NULL,
      variant_no           TINYINT UNSIGNED NULL,
      kashrut_type         ENUM('MEAT','DAIRY','PARVE','UNKNOWN') NOT NULL DEFAULT 'UNKNOWN',
      holiday_flags        SET('PASSOVER','SABBATH','PURIM','HANUKKAH') NULL,
      primary_ingredient   VARCHAR(255) NULL,
      methods              SET('BAKED','BOILED','BROILED','FRIED','SAUTEED','SCALLOPED',
                               'STEAMED','STEWED','ROASTED','GRILLED','RAW','PICKLED', 'POACHED') NULL,
      yield_text           VARCHAR(255) NULL,
      servings             INT NULL,
      index_terms          TEXT NULL,
      source_ref           VARCHAR(255) NOT NULL DEFAULT 'Project Gutenberg – The International Jewish Cook Book',
      created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (recipe_id),
      FULLTEXT KEY ft_title (title),
      KEY ix_section_order (section, order_in_section)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_520_ci;
    """,

    # 2) recipe_segments
    """
    CREATE TABLE IF NOT EXISTS recipe_segments (
      segment_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      recipe_id      BIGINT UNSIGNED NOT NULL,
      segment_type   ENUM('TITLE','INGREDIENT','STEP','HEADNOTE','NOTE') NOT NULL,
      segment_index  INT NOT NULL,
      token_count    INT NOT NULL,
      PRIMARY KEY (segment_id),
      UNIQUE KEY uq_segment (recipe_id, segment_type, segment_index),
      KEY ix_recipe_type (recipe_id, segment_type),
      CONSTRAINT fk_segments_recipe
        FOREIGN KEY (recipe_id) REFERENCES recipes(recipe_id)
        ON DELETE CASCADE ON UPDATE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_520_ci;
    """,

    # 3) words
    """
    CREATE TABLE IF NOT EXISTS words (
      word_id   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      word      VARCHAR(191) NOT NULL,
      is_stop   TINYINT(1) NOT NULL DEFAULT 0,
      PRIMARY KEY (word_id),
      UNIQUE KEY uq_word (word)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_520_ci;
    """,

    # 4) segment_tokens
    """
    CREATE TABLE IF NOT EXISTS segment_tokens (
      token_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      segment_id   BIGINT UNSIGNED NOT NULL,
      word_order   INT NOT NULL,
      word_id      BIGINT UNSIGNED NOT NULL,
      joiner       VARCHAR(16) NULL,
      PRIMARY KEY (token_id),
      UNIQUE KEY uq_seg_pos (segment_id, word_order),
      KEY ix_token_word (word_id),
      CONSTRAINT fk_tok_segment
        FOREIGN KEY (segment_id) REFERENCES recipe_segments(segment_id)
        ON DELETE CASCADE ON UPDATE RESTRICT,
      CONSTRAINT fk_tok_word
        FOREIGN KEY (word_id)    REFERENCES words(word_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_520_ci;
    """,

    # 5) word_groups
    """
    CREATE TABLE IF NOT EXISTS word_groups (
      group_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      group_name   VARCHAR(191) NOT NULL,
      description  TEXT NULL,
      PRIMARY KEY (group_id),
      UNIQUE KEY uq_group_name (group_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_520_ci;
    """,

    # 6) word_group_items
    """
    CREATE TABLE IF NOT EXISTS word_group_items (
      item_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      group_id   BIGINT UNSIGNED NOT NULL,
      word_id    BIGINT UNSIGNED NOT NULL,
      recipe_id  BIGINT UNSIGNED NULL,
      scope      ENUM('TITLE','INGREDIENT','STEP','GLOBAL') NOT NULL DEFAULT 'STEP',
      PRIMARY KEY (item_id),
      UNIQUE KEY uq_group_word_recipe (group_id, word_id, recipe_id),
      KEY ix_wgi_word (word_id),
      KEY ix_wgi_recipe (recipe_id),
      CONSTRAINT fk_wgi_group
        FOREIGN KEY (group_id)  REFERENCES word_groups(group_id)
        ON DELETE CASCADE ON UPDATE RESTRICT,
      CONSTRAINT fk_wgi_word
        FOREIGN KEY (word_id)   REFERENCES words(word_id)
        ON DELETE CASCADE ON UPDATE RESTRICT,
      CONSTRAINT fk_wgi_recipe
        FOREIGN KEY (recipe_id) REFERENCES recipes(recipe_id)
        ON DELETE CASCADE ON UPDATE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_520_ci;
    """,

    # 7) expressions
    """
    CREATE TABLE IF NOT EXISTS expressions (
      expression_id       BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      name                VARCHAR(191) NOT NULL,
      description         TEXT NULL,
      is_order_sensitive  TINYINT(1) NOT NULL DEFAULT 1,
      max_gap             TINYINT UNSIGNED NOT NULL DEFAULT 0,
      PRIMARY KEY (expression_id),
      UNIQUE KEY uq_expression_name_gap (name, max_gap)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_520_ci;
    """,

    # 8) expression_terms
    """
    CREATE TABLE IF NOT EXISTS expression_terms (
      expression_id   BIGINT UNSIGNED NOT NULL,
      position_index  INT NOT NULL,
      word_id         BIGINT UNSIGNED NOT NULL,
      PRIMARY KEY (expression_id, position_index),
      KEY ix_expr_word (word_id),
      CONSTRAINT fk_et_expr FOREIGN KEY (expression_id) REFERENCES expressions(expression_id)
        ON DELETE CASCADE ON UPDATE RESTRICT,
      CONSTRAINT fk_et_word FOREIGN KEY (word_id) REFERENCES words(word_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_520_ci;
    """,
]

def bootstrap_db():
    """
    Create database and all tables if they don't exist.
    Safe to call multiple times (idempotent).
    """
    # 1) Ensure database exists
    with _connect_server() as srv, srv.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{config.DB_NAME}` "
            "DEFAULT CHARACTER SET utf8mb4 "
            "DEFAULT COLLATE utf8mb4_unicode_520_ci"
        )

    # 2) Ensure tables exist (connect to DB)
    conn = None
    try:
        conn = pymysql.connect(
            host=config.DB_HOST,
            port=config.DB_PORT,
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            database=config.DB_NAME,
            autocommit=True,   # DDL convenience
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
        )
        with conn.cursor() as cur:
            for statement in DDL:
                cur.execute(statement)
            
            # Migration: Update expressions table unique constraint if needed
            try:
                cur.execute("""
                    SELECT CONSTRAINT_NAME 
                    FROM information_schema.TABLE_CONSTRAINTS 
                    WHERE TABLE_SCHEMA = %s 
                    AND TABLE_NAME = 'expressions' 
                    AND CONSTRAINT_NAME = 'uq_expression_name'
                """, (config.DB_NAME,))
                if cur.fetchone():
                    # Old constraint exists, migrate it
                    cur.execute("ALTER TABLE expressions DROP INDEX uq_expression_name")
                    cur.execute("ALTER TABLE expressions ADD UNIQUE KEY uq_expression_name_gap (name, max_gap)")
            except Exception:
                # Constraint doesn't exist or already migrated, ignore
                pass
            
            # Migration: Add GLOBAL to word_group_items scope enum if needed
            try:
                cur.execute("""
                    SELECT COLUMN_TYPE 
                    FROM information_schema.COLUMNS 
                    WHERE TABLE_SCHEMA = %s 
                    AND TABLE_NAME = 'word_group_items' 
                    AND COLUMN_NAME = 'scope'
                """, (config.DB_NAME,))
                result = cur.fetchone()
                if result and 'GLOBAL' not in result.get('COLUMN_TYPE', ''):
                    # GLOBAL not in enum, add it
                    cur.execute("ALTER TABLE word_group_items MODIFY scope ENUM('TITLE','INGREDIENT','STEP','GLOBAL') NOT NULL DEFAULT 'STEP'")
            except Exception:
                # Column doesn't exist or already migrated, ignore
                pass
    finally:
        if conn:
            conn.close()
