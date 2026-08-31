import os
import json
import logging
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("db_logger")

_connection_pool = None

def get_db_config():
    """Reads database configuration from environment variables with sensible defaults."""
    return {
        "host": os.environ.get("DB_HOST", "localhost"),
        "port": os.environ.get("DB_PORT", "5432"),
        "database": os.environ.get("DB_NAME", "insurance_db"),
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", "ac3r")
    }

def init_db_pool():
    """Initializes the connection pool and creates the table if it does not exist."""
    global _connection_pool
    if _connection_pool is not None:
        return

    config = get_db_config()
    try:
        logger.info(f"Connecting to database '{config['database']}' on {config['host']}:{config['port']} as user '{config['user']}'...")
        _connection_pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            **config
        )
        # Verify connection and initialize table
        create_tables_if_not_exists()
    except Exception as e:
        logger.error(f"Failed to initialize database connection pool: {e}")
        # We do not crash the application immediately, but log the error.
        _connection_pool = None

def create_tables_if_not_exists():
    """Creates the processed_documents table if it doesn't already exist."""
    conn = get_connection()
    if conn is None:
        logger.warning("Database connection unavailable. Skipping table initialization.")
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_documents (
                    id SERIAL PRIMARY KEY,
                    filename VARCHAR(255) NOT NULL,
                    doc_type VARCHAR(50) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    extracted_data JSONB,
                    meta_data JSONB,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                
                -- Add an index on doc_type for faster filtering
                CREATE INDEX IF NOT EXISTS idx_processed_docs_doc_type ON processed_documents(doc_type);
                
                -- Add a GIN index on extracted_data for efficient JSON querying
                CREATE INDEX IF NOT EXISTS idx_processed_docs_extracted_data ON processed_documents USING gin(extracted_data);
            """)
            conn.commit()
            logger.info("Database tables and indexes initialized successfully.")
    except Exception as e:
        conn.rollback()
        logger.error(f"Error initializing database tables: {e}")
    finally:
        put_connection(conn)

def get_connection():
    """Gets a connection from the pool."""
    global _connection_pool
    if _connection_pool is None:
        init_db_pool()
        if _connection_pool is None:
            return None
    try:
        return _connection_pool.getconn()
    except Exception as e:
        logger.error(f"Error getting connection from pool: {e}")
        return None

def put_connection(conn):
    """Returns a connection to the pool."""
    global _connection_pool
    if _connection_pool is not None and conn is not None:
        try:
            _connection_pool.putconn(conn)
        except Exception as e:
            logger.error(f"Error returning connection to pool: {e}")

def save_extraction_result(filename: str, doc_type: str, status: str, validated_data: dict) -> int:
    """
    Saves the extraction results to the processed_documents table.
    Splits the main extraction fields and the metadata companion object (_extraction_meta).
    
    Returns the inserted record's ID, or -1 if insertion fails.
    """
    conn = get_connection()
    if conn is None:
        logger.error("Cannot save extraction result: Database connection is unavailable.")
        return -1

    # Extract _extraction_meta from the dict if present
    extracted_data = dict(validated_data)
    meta_data = extracted_data.pop("_extraction_meta", {})

    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO processed_documents (filename, doc_type, status, extracted_data, meta_data)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
            """, (
                filename,
                doc_type,
                status,
                psycopg2.extras.Json(extracted_data),
                psycopg2.extras.Json(meta_data)
            ))
            row = cur.fetchone()
            conn.commit()
            record_id = row[0] if row else -1
            logger.info(f"Successfully saved extraction result for file '{filename}' with ID: {record_id}")
            return record_id
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save extraction result for file '{filename}': {e}")
        return -1
    finally:
        put_connection(conn)

def get_extraction_results(limit: int = 100):
    """Helper query to fetch the latest stored extractions."""
    conn = get_connection()
    if conn is None:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, filename, doc_type, status, extracted_data, meta_data, created_at 
                FROM processed_documents 
                ORDER BY created_at DESC 
                LIMIT %s;
            """, (limit,))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Failed to fetch extraction results: {e}")
        return []
    finally:
        put_connection(conn)
