import type {
  FontCatalog,
  CatalogRecord,
  CatalogStyleRecord,
  CatalogRequestRecord,
  Style,
} from '../types/catalog';

export class CatalogService {
  constructor(private readonly db: D1Database) {}

  async getCatalogByCanonicalKey(canonicalKey: string): Promise<FontCatalog | null> {
    const catalog = await this.db
      .prepare('SELECT * FROM catalogs WHERE canonical_key = ?')
      .bind(canonicalKey)
      .first<CatalogRecord>();

    if (!catalog) return null;

    const stylesResult = await this.db
      .prepare('SELECT * FROM catalog_styles WHERE catalog_id = ? ORDER BY id ASC')
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
      .prepare('SELECT * FROM catalog_styles WHERE catalog_id = ? ORDER BY id ASC')
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

    // Check if an active request already exists for this user and canonical key
    const existing = await this.db
      .prepare(
        'SELECT * FROM catalog_requests WHERE user_id = ? AND canonical_key = ? ORDER BY created_at DESC LIMIT 1'
      )
      .bind(userId, canonicalKey)
      .first<CatalogRequestRecord>();

    if (existing) {
      return existing;
    }

    // Check if catalog already exists
    const catalog = await this.db
      .prepare('SELECT id FROM catalogs WHERE canonical_key = ?')
      .bind(canonicalKey)
      .first<{ id: string }>();

    const requestId = crypto.randomUUID();
    const status = catalog ? 'COMPLETED' : 'PENDING';
    const catalogId = catalog ? catalog.id : null;

    await this.db
      .prepare(
        `INSERT INTO catalog_requests (id, user_id, canonical_key, source_url, status, catalog_id, created_at, updated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
      )
      .bind(requestId, userId, canonicalKey, sourceUrl, status, catalogId, now, now)
      .run();

    return {
      id: requestId,
      user_id: userId,
      canonical_key: canonicalKey,
      source_url: sourceUrl,
      status,
      catalog_id: catalogId || undefined,
      created_at: now,
      updated_at: now,
    };
  }

  async persistCatalogResult(catalog: FontCatalog): Promise<string> {
    const now = Date.now();
    const catalogId = crypto.randomUUID();

    // Insert or update catalog
    const existing = await this.db
      .prepare('SELECT id FROM catalogs WHERE canonical_key = ?')
      .bind(catalog.canonicalKey)
      .first<{ id: string }>();

    const activeId = existing ? existing.id : catalogId;

    if (existing) {
      await this.db
        .prepare(
          `UPDATE catalogs SET source_url = ?, family_name = ?, foundry = ?, updated_at = ? WHERE id = ?`
        )
        .bind(catalog.sourceUrl, catalog.familyName, catalog.foundry || null, now, activeId)
        .run();
    } else {
      await this.db
        .prepare(
          `INSERT INTO catalogs (id, source_url, canonical_key, family_name, foundry, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)`
        )
        .bind(
          activeId,
          catalog.sourceUrl,
          catalog.canonicalKey,
          catalog.familyName,
          catalog.foundry || null,
          now,
          now
        )
        .run();
    }

    // Upsert styles
    for (const style of catalog.styles) {
      const styleRowId = `style_${activeId}_${style.id}`;
      const price = style.price !== undefined ? style.price : 50000;
      await this.db
        .prepare(
          `INSERT INTO catalog_styles (id, catalog_id, style_id, display_name, price, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(catalog_id, style_id) DO UPDATE SET display_name = excluded.display_name, price = excluded.price`
        )
        .bind(styleRowId, activeId, style.id, style.displayName, price, now)
        .run();
    }

    // Update pending requests for this canonical key
    await this.db
      .prepare(
        `UPDATE catalog_requests SET status = 'COMPLETED', catalog_id = ?, updated_at = ?
         WHERE canonical_key = ? AND status = 'PENDING'`
      )
      .bind(activeId, now, catalog.canonicalKey)
      .run();

    return activeId;
  }
}
