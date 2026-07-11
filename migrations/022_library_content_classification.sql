-- Migration 022: library content classification columns
-- Adds content_class, quality_score, classified_at to library.conversations
-- Used by classify_library_content.py (Haiku batch tagger) and hybrid search RRF

BEGIN;

ALTER TABLE library.conversations
    ADD COLUMN IF NOT EXISTS content_class TEXT DEFAULT 'unknown'
        CHECK (content_class IN ('tutorial','qa','review','performance','talk','other','unknown')),
    ADD COLUMN IF NOT EXISTS quality_score NUMERIC(3,2) DEFAULT 0.50
        CHECK (quality_score >= 0.00 AND quality_score <= 1.00),
    ADD COLUMN IF NOT EXISTS classified_at TIMESTAMPTZ;

-- integrity: classified_at must be set when content_class is known
ALTER TABLE library.conversations
    ADD CONSTRAINT chk_classification_integrity
        CHECK (
            (content_class = 'unknown' AND classified_at IS NULL)
            OR
            (content_class != 'unknown' AND classified_at IS NOT NULL)
        );

-- index for filtering by class (used in search WHERE content_class = ANY($n))
CREATE INDEX IF NOT EXISTS idx_library_conv_content_class
    ON library.conversations (content_class)
    WHERE content_class != 'unknown';

COMMENT ON COLUMN library.conversations.content_class IS
    'Haiku-assigned content category: tutorial|qa|review|performance|talk|other|unknown';
COMMENT ON COLUMN library.conversations.quality_score IS
    'Haiku-assigned quality 0.00–1.00 (NUMERIC to avoid float precision issues)';
COMMENT ON COLUMN library.conversations.classified_at IS
    'Timestamp of last classification run';

COMMIT;
