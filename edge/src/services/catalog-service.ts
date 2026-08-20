import type {
  FontCatalog,
  CatalogRecord,
  CatalogStyleRecord,
  CatalogRequestRecord,
  Style,
} from '../types/catalog';

function deterministicCatalogId(canonicalKey: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < canonicalKey.length; i++) {
    hash ^= canonicalKey.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  const hex = (hash >>> 0).toString(16).padStart(8, '0');
  const sanitized = canonicalKey.replace(/[^a-zA-Z0-9]/g, '_').slice(0, 30);
  return `cat_${sanitized}_${hex}`;
}

export class CatalogService {
  constructor(private readonly db: D1Database) {}

  async getCatalogByCanonicalKey(canonicalKey: string): Promise<FontCatalog | null> {
    const catalog = await this.db
      .prepare('SELECT * FROM catalogs WHERE canonical_key = ?')
      .bind(canonicalKey)
      .first<CatalogRecord>();

    if (!catalog) return null;

    const stylesResult = await this.db
      .prepare('SELECT * FROM catalog_styles WHERE catalog_id = ? ORDER BY rowid ASC')
      .bind(catalog.id)
      .all<CatalogStyleRecord>();

    const styles: Style[] = (stylesResult.results || []).map((s) => ({
      id: s.style_id,
      displayName: s.display_name,
      price: s.price,
    }));

    return {
      sourceUrl: catalog.source_url,
      canonicalKey: catalog.canonical_key,
      familyName: catalog.family_name,
      foundry: catalog.foundry || undefined,
      styles,
    };
  }

  async getCatalogById(catalogId: string): Promise<FontCatalog | null> {
    const catalog = await this.db
      .prepare('SELECT * FROM catalogs WHERE id = ?')
      .bind(catalogId)
      .first<CatalogRecord>();

    if (!catalog) return null;

    const stylesResult = await this.db
      .prepare('SELECT * FROM catalog_styles WHERE catalog_id = ? ORDER BY rowid ASC')
      .bind(catalog.id)
      .all<CatalogStyleRecord>();

    const styles: Style[] = (stylesResult.results || []).map((s) => ({
      id: s.style_id,
      displayName: s.display_name,
      price: s.price,
    }));

    return {
      sourceUrl: catalog.source_url,
      canonicalKey: catalog.canonical_key,
      familyName: catalog.family_name,
      foundry: catalog.foundry || undefined,
      styles,
    };
  }

  async getOrCreateCatalogRequest(
    userId: string,
    sourceUrl: string,
    canonicalKey: string
  ): Promise<CatalogRequestRecord> {
    const now = Date.now();

    // Check if catalog already exists
    const catalog = await this.db
      .prepare('SELECT id FROM catalogs WHERE canonical_key = ?')
      .bind(canonicalKey)
      .first<{ id: string }>();

    const requestId = crypto.randomUUID();
    const status = catalog ? 'COMPLETED' : 'PENDING';
    const catalogId = catalog ? catalog.id : null;

    // Conflict-safe atomic upsert ensuring concurrent requests never duplicate rows
    await this.db
      .prepare(
        `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, catalog_id, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(user_id, canonical_key) DO UPDATE SET
           source_url = excluded.source_url,
           status = CASE WHEN catalog_requests.status = 'COMPLETED' THEN 'COMPLETED' ELSE excluded.status END,
           catalog_id = COALESCE(catalog_requests.catalog_id, excluded.catalog_id),
           updated_at = excluded.updated_at`
      )
      .bind(requestId, userId, canonicalKey, sourceUrl, status, catalogId, now, now)
      .run();

    const recorded = await this.db
      .prepare('SELECT * FROM catalog_requests WHERE user_id = ? AND canonical_key = ?')
      .bind(userId, canonicalKey)
      .first<CatalogRequestRecord>();

    return recorded!;
  }

  async persistCatalogResult(catalog: FontCatalog): Promise<string> {
    const now = Date.now();
    const catalogId = deterministicCatalogId(catalog.canonicalKey);
    const statements: D1PreparedStatement[] = [];

    // Deterministic upsert ensuring concurrent first-creations resolve idempotently
    statements.push(
      this.db
        .prepare(
          `INSERT INTO catalogs (id, source_url, canonical_key, family_name, foundry, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(canonical_key) DO UPDATE SET
             source_url = excluded.source_url,
             family_name = excluded.family_name,
             foundry = excluded.foundry,
             updated_at = excluded.updated_at`
        )
        .bind(
          catalogId,
          catalog.sourceUrl,
          catalog.canonicalKey,
          catalog.familyName,
          catalog.foundry || null,
          now,
          now
        )
    );

    // Atomically purge old styles so re-persisting does not retain stale styles
    statements.push(
      this.db
        .prepare('DELETE FROM catalog_styles WHERE catalog_id = ?')
        .bind(catalogId)
    );

    // Insert authoritative style set
    for (const style of catalog.styles) {
      const styleRowId = `style_${catalogId}_${style.id}`;
      const price = style.price !== undefined ? style.price : 50000;
      statements.push(
        this.db
          .prepare(
            `INSERT INTO catalog_styles (id, catalog_id, style_id, display_name, price, created_at)
             VALUES (?, ?, ?, ?, ?, ?)`
          )
          .bind(styleRowId, catalogId, style.id, style.displayName, price, now)
      );
    }

    // Mark pending requests as COMPLETED
    statements.push(
      this.db
        .prepare(
          `UPDATE catalog_requests SET status = 'COMPLETED', catalog_id = ?, updated_at = ?
           WHERE canonical_key = ? AND status = 'PENDING'`
        )
        .bind(catalogId, now, catalog.canonicalKey)
    );

    // Commit atomically in a single D1 batch
    await this.db.batch(statements);

    return catalogId;
  }
}
