-- =============================================================================
-- 0002 - private storage bucket for uploaded files
-- =============================================================================
-- Phase 4 of the Enterprise AI Intelligence Platform.
--
-- Runs after 0001. Also safe to re-run.
--
-- Object keys follow a fixed convention:
--
--     <owner_id>/<document_id>/<filename>
--     e.g. 8f14e45f-..._.../3c9a.../q3-report.pdf
--
-- The first path segment is the owner's uuid. That is what makes the storage
-- policies below a single string comparison: no join back to `documents`, and no
-- way for a crafted filename to escape, because the owner segment is written by
-- the server from the verified token rather than taken from the upload.
--
-- The bucket is PRIVATE. The API never hands out public URLs; files are fetched
-- through the backend, which is where the ownership check already lives.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Bucket
-- -----------------------------------------------------------------------------
-- file_size_limit mirrors MAX_UPLOAD_MB (25 by default). Keep the two in step:
-- the bucket rejects first, and its rejection is less informative than the
-- application's 413.
--
-- allowed_mime_types is safe to keep strict because the *backend* uploads the
-- object and sets Content-Type explicitly from the validated extension. A
-- browser-supplied type never reaches this column. The list is the set of
-- formats app/security/file_validation.py accepts.
-- -----------------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'documents',
    'documents',
    false,
    26214400,
    array[
        'application/pdf',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'text/plain',
        'text/markdown',
        'text/csv'
    ]
)
on conflict (id) do update
    set public             = excluded.public,
        file_size_limit    = excluded.file_size_limit,
        allowed_mime_types = excluded.allowed_mime_types;


-- -----------------------------------------------------------------------------
-- Policies on storage.objects
-- -----------------------------------------------------------------------------
-- storage.objects already has RLS enabled by Supabase, so these only add the
-- rules. As with the table policies, every one is scoped to the owner's own
-- folder and to the `authenticated` role - the public `anon` role gets nothing.
--
-- Note that a policy on storage.objects can only reference the object's key and
-- bucket, not the uploader, so the key layout is doing the security work here.
-- -----------------------------------------------------------------------------

drop policy if exists documents_storage_select_own on storage.objects;
create policy documents_storage_select_own on storage.objects
    for select to authenticated
    using (
        bucket_id = 'documents'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );

drop policy if exists documents_storage_insert_own on storage.objects;
create policy documents_storage_insert_own on storage.objects
    for insert to authenticated
    with check (
        bucket_id = 'documents'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );

-- UPDATE and DELETE both need WITH CHECK as well as USING: a row that passes the
-- USING test could otherwise be rewritten into a key outside the owner's folder.
drop policy if exists documents_storage_update_own on storage.objects;
create policy documents_storage_update_own on storage.objects
    for update to authenticated
    using (
        bucket_id = 'documents'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    )
    with check (
        bucket_id = 'documents'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );

drop policy if exists documents_storage_delete_own on storage.objects;
create policy documents_storage_delete_own on storage.objects
    for delete to authenticated
    using (
        bucket_id = 'documents'
        and (storage.foldername(name))[1] = (select auth.uid())::text
    );


notify pgrst, 'reload schema';
