export interface Style {
  id: string; // Stable id/hash
  displayName: string;
  price?: number;
}

export interface FontCatalog {
  sourceUrl: string;
  canonicalKey: string;
  familyName: string;
  foundry?: string;
  styles: Style[];
}

export interface CatalogRecord {
  id: string;
  source_url: string;
  canonical_key: string;
  family_name: string;
  foundry?: string;
  created_at: number;
  updated_at: number;
}

export interface CatalogStyleRecord {
  id: string;
  catalog_id: string;
  style_id: string;
  display_name: string;
  price: number;
  created_at: number;
}

export interface CatalogRequestRecord {
  id: string;
  user_id: string;
  canonical_key: string;
  source_url: string;
  status: 'PENDING' | 'COMPLETED' | 'FAILED';
  catalog_id?: string;
  created_at: number;
  updated_at: number;
}
