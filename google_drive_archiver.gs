/**
 * Flow Try-On Factory — Google Drive Archiver (R12)
 *
 * Product-first archive structure:
 *   Flow Try-On Archive /
 *     Product Name /
 *       YYYY-MM-DD /
 *         references /
 *         try-ons /
 *         videos /
 *
 * Deploy this Apps Script as a Web App that EXECUTES AS YOU and is accessible
 * to Anyone. Streamlit authenticates each request with ARCHIVE_SECRET.
 *
 * Script Properties required:
 *   ARCHIVE_SECRET     = same long random secret used by Streamlit
 *   ARCHIVE_FOLDER_ID  = ID of the root "Flow Try-On Archive" Drive folder
 */

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function cleanName(value, fallback) {
  const out = String(value || '')
    .replace(/[\\/:*?"<>|]/g, '_')
    .replace(/\s+/g, ' ')
    .trim();
  return (out || fallback || 'Flow Try-On').slice(0, 120);
}

function getOrCreateFolder(parent, name) {
  const safe = cleanName(name, 'Flow Try-On');
  const matches = parent.getFoldersByName(safe);
  if (matches.hasNext()) return matches.next();
  return parent.createFolder(safe);
}

function folderUrl(folder) {
  return 'https://drive.google.com/drive/folders/' + folder.getId();
}

function filePayload(file, productFolder, dateFolder, mediaFolder, existing) {
  const id = file.getId();
  return {
    ok: true,
    existing: Boolean(existing),
    file_id: id,
    name: file.getName(),
    view_url: file.getUrl(),
    download_url: 'https://drive.google.com/uc?export=download&id=' + encodeURIComponent(id),
    product_folder_id: productFolder.getId(),
    product_folder_url: folderUrl(productFolder),
    batch_folder_id: dateFolder.getId(),
    batch_folder_url: folderUrl(dateFolder),
    media_folder_id: mediaFolder.getId(),
    media_folder_url: folderUrl(mediaFolder)
  };
}

function doGet() {
  return jsonResponse({ok: true, service: 'Flow Try-On Google Drive Archiver R12'});
}

function doPost(e) {
  try {
    const props = PropertiesService.getScriptProperties();
    const expectedSecret = String(props.getProperty('ARCHIVE_SECRET') || '');
    const rootFolderId = String(props.getProperty('ARCHIVE_FOLDER_ID') || '');

    if (!expectedSecret || !rootFolderId) {
      return jsonResponse({ok: false, error: 'Apps Script properties are not configured.'});
    }

    const body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (!body.secret || String(body.secret) !== expectedSecret) {
      return jsonResponse({ok: false, error: 'Unauthorized archive request.'});
    }

    // R12 can reopen old batches even when Flow/CDN links have expired.
    // The private Drive file is read through this same secret-authenticated bridge.
    if (String(body.action || '').toLowerCase() === 'read') {
      if (!body.file_id) return jsonResponse({ok: false, error: 'Missing Drive file ID.'});
      const stored = DriveApp.getFileById(String(body.file_id));
      const blob = stored.getBlob();
      const bytes = blob.getBytes();
      if (bytes.length > 32 * 1024 * 1024) {
        return jsonResponse({ok: false, error: 'Archived file is larger than 32 MB; use the Drive link directly.'});
      }
      return jsonResponse({
        ok: true,
        file_id: stored.getId(),
        name: stored.getName(),
        mime_type: blob.getContentType(),
        data_base64: Utilities.base64Encode(bytes)
      });
    }

    if (!body.data_base64 || !body.filename) {
      return jsonResponse({ok: false, error: 'Missing file data or filename.'});
    }

    const root = DriveApp.getFolderById(rootFolderId);
    const productName = body.product_name || body.batch_name || 'Flow Try-On Product';
    const batchDate = body.batch_date || Utilities.formatDate(new Date(), Session.getScriptTimeZone() || 'UTC', 'yyyy-MM-dd');
    const productFolder = getOrCreateFolder(root, productName);
    const dateFolder = getOrCreateFolder(productFolder, batchDate);

    const rawKind = String(body.kind || '').toLowerCase();
    const folderName = rawKind === 'video' ? 'videos' : (rawKind === 'reference' ? 'references' : 'try-ons');
    const mediaFolder = getOrCreateFolder(dateFolder, folderName);
    const fallbackName = rawKind === 'video' ? 'video.mp4' : 'image.jpg';
    const filename = cleanName(body.filename, fallbackName);

    // Deterministic filenames make retries and Streamlit redeploys idempotent.
    const existing = mediaFolder.getFilesByName(filename);
    if (existing.hasNext()) {
      return jsonResponse(filePayload(existing.next(), productFolder, dateFolder, mediaFolder, true));
    }

    const bytes = Utilities.base64Decode(String(body.data_base64));
    const mimeType = String(body.mime_type || (rawKind === 'video' ? 'video/mp4' : 'image/jpeg'));
    const blob = Utilities.newBlob(bytes, mimeType, filename);
    const file = mediaFolder.createFile(blob);
    if (body.description) file.setDescription(String(body.description).slice(0, 1000));

    return jsonResponse(filePayload(file, productFolder, dateFolder, mediaFolder, false));
  } catch (err) {
    return jsonResponse({ok: false, error: String(err && err.message ? err.message : err)});
  }
}
