-- Runs once on first start of the postgres volume.
CREATE ROLE appuser LOGIN PASSWORD 'apppass';

CREATE TABLE items (
    id         serial PRIMARY KEY,
    name       text NOT NULL,
    price      numeric(10, 2) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO items (name, price)
SELECT 'item-' || g, round((random() * 100)::numeric, 2)
FROM generate_series(1, 2000) AS g;

GRANT SELECT ON items TO appuser;
