/**
 * Flow Try-On Factory — Google Drive Archiver
 *
 * Deploy this Apps Script as a Web App that EXECUTES AS YOU and is accessible
 * to Anyone. The Streamlit app authenticates each request with a long shared
 * secret stored in Script Properties and Streamlit Secrets.
 *
 * Script Properties required:
 *   ARCHIVE_SECRET     = the same long random secret used by Streamlit
 *   ARCHIVE_FOLDER_ID  = the ID of the Google Drive folder that will hold batches
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

function filePayload(file, batchFolder, existing) {
  const id = file.getId();
  return {
    ok: true,
    existing: Boolean(existing),
    file_id: id,
    name: file.getName(),
    view_url: file.getUrl(),
    download_url: 'https://drive.google.com/uc?export=download&id=' + encodeURIComponent(id),
    batch_folder_id: batchFolder.getId(),
    batch_folder_url: 'https://drive.google.com/drive/folders/' + batchFolder.getId()
  };
}

function doGet() {
  return jsonResponse({ok: true, service: 'Flow Try-On Google Drive Archiver'});
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
    if (!body.data_base64 || !body.filename) {
      return jsonResponse({ok: false, error: 'Missing file data or filename.'});
    }

    const root = DriveApp.getFolderById(rootFolderId);
    const batchFolder = getOrCreateFolder(root, body.batch_name || 'Flow Try-On');
    const kind = String(body.kind || '').toLowerCase() === 'video' ? 'videos' : 'images';
    const mediaFolder = getOrCreateFolder(batchFolder, kind);
    const filename = cleanName(body.filename, kind === 'videos' ? 'video.mp4' : 'image.jpg');

    // Deterministic filenames make retries and Streamlit redeploys idempotent.
    const existing = mediaFolder.getFilesByName(filename);
    if (existing.hasNext()) {
      return jsonResponse(filePayload(existing.next(), batchFolder, true));
    }

    const bytes = Utilities.base64Decode(String(body.data_base64));
    const mimeType = String(body.mime_type || (kind === 'videos' ? 'video/mp4' : 'image/jpeg'));
    const blob = Utilities.newBlob(bytes, mimeType, filename);
    const file = mediaFolder.createFile(blob);
    if (body.description) file.setDescription(String(body.description).slice(0, 1000));

    return jsonResponse(filePayload(file, batchFolder, false));
  } catch (err) {
    return jsonResponse({ok: false, error: String(err && err.message ? err.message : err)});
  }
}
