-- topic clustering: dominant_topic 文字列を意味クラスタにまとめる layer。
-- recluster_topics.py が atomic に rebuild する想定 (cluster_id は ephemeral)。

CREATE TABLE IF NOT EXISTS personal.topic_clusters (
    cluster_id        INT PRIMARY KEY,
    label             TEXT NOT NULL,                 -- medoid topic 文字列
    n_member_topics   INT NOT NULL,
    n_member_convs    INT NOT NULL,
    method            TEXT NOT NULL DEFAULT 'leaf_mcs3',
    rescue_threshold  REAL,
    rebuilt_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE personal.conversations
    ADD COLUMN IF NOT EXISTS topic_cluster_id  INT
        REFERENCES personal.topic_clusters(cluster_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS topic_cluster_sim REAL;

CREATE INDEX IF NOT EXISTS idx_conv_topic_cluster
    ON personal.conversations(topic_cluster_id);
