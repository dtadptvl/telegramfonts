-- Migration: 0006_artifact_parts.sql
-- D1 Migration for Multipart Artifact Delivery and Ordered Part Storage

ALTER TABLE fulfillment_receipts ADD COLUMN artifact_parts TEXT;
ALTER TABLE fulfillment_jobs ADD COLUMN artifact_parts TEXT;
