const SAST_TIMEZONE = "Africa/Johannesburg";
const REPO_OWNER = "hlvanvreden-ai";
const REPO_NAME = "Bible_study_youtube";

function formatSAST(dateObj) {
  return Utilities.formatDate(dateObj, SAST_TIMEZONE, "yyyy-MM-dd'T'HH:mm:ss'+02:00'");
}

function safeNotify(message) {
  Logger.log(message);
  try {
    SpreadsheetApp.getUi().alert(message);
  } catch (e) {
    // Running from Apps Script editor or time-driven trigger where UI is unavailable
  }
}

/**
 * Custom Spreadsheet Menu — includes every stage of the pipeline in order.
 */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("🎬 Video Pipeline")
    .addItem("🚀 Run Full Pipeline in One Go", "runFullPipelineOneGo")
    .addSeparator()
    .addItem("1. Fetch New Studies from NotebookLM", "manualFetchFromNotebook")
    .addItem("2. Prepare Render Metadata (Creative Brief & Slides)", "manualPrepareMetadata")
    .addItem("3. Render Next Video (Unlisted)", "manualRunCompletePipeline")
    .addItem("4. Send Approval Emails (Review → Pending Review)", "manualSendApprovalEmails")
    .addItem("5. Schedule Approved Videos on YouTube", "manualScheduleApprovedVideos")
    .addSeparator()
    .addItem("Fill Missing Ready Dates & Templates (SAST)", "fillMissingReadyDates")
    .addItem("Reset Template Rotation to Row 2", "resetTemplateCounter")
    .addSeparator()
    .addItem("⚙️ Enable Automated Trigger (Every 6h)", "setupAutomaticTriggers")
    .addItem("🛑 Disable Automated Triggers", "removeAutomaticTriggers")
    .addToUi();
}

/**
 * Runs the entire end-to-end pipeline in sequence:
 * 1. Fetches new studies from NotebookLM via GitHub Actions
 * 2. Prepares render metadata (Creative Brief, Slide Breakdown, Themes, Media)
 * 3. Triggers Stage 1 video rendering & Unlisted YouTube upload + Review Email
 */
function runFullPipelineOneGo() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Sheet1");
  const initialLastRow = sheet.getLastRow();

  // Step 1: Trigger NotebookLM sync on GitHub Actions
  dispatchGitHubEvent("fetch_notebook_studies", true);

  // Wait up to 45 seconds for GitHub Actions to append any new studies (polling every 5s)
  for (let attempt = 0; attempt < 9; attempt++) {
    Utilities.sleep(5000);
    SpreadsheetApp.flush();
    if (sheet.getLastRow() > initialLastRow) {
      break;
    }
  }

  // Step 2: Prepare all blank/Not Started rows with Templates + Drive Media & Metadata
  const lastRow = sheet.getLastRow();
  const sastDate = formatSAST(new Date(Date.now() + 30 * 60 * 1000));
  let preparedCount = 0;
  let lastMedia = null;

  for (let r = 2; r <= lastRow; r++) {
    const titleVal = String(sheet.getRange(r, 2).getValue() || "").trim();
    if (!titleVal) continue;

    const statusCell = sheet.getRange(r, 7);
    const dateCell = sheet.getRange(r, 8);
    const musicCell = sheet.getRange(r, 15);
    const bgCell = sheet.getRange(r, 16);
    const status = String(statusCell.getValue() || "").trim();

    if (status === "" || status === "Not Started") {
      statusCell.setValue("Ready");
      if (!dateCell.getValue()) dateCell.setValue(sastDate);
    }

    if (
      String(statusCell.getValue()).trim() === "Ready" &&
      (!musicCell.getValue() || !bgCell.getValue())
    ) {
      lastMedia = applyTemplateAndDriveMedia(sheet, r);
      preparedCount++;
    }
  }

  // Step 2b: Trigger Python preparation metadata script workflow
  dispatchGitHubEvent("prepare_study_metadata", true);

  // Step 3: Trigger Stage 1 Video Render & Review Email
  dispatchGitHubEvent("build_daily_video", true);

  const mediaInfo = lastMedia
    ? `\nBackground: ${lastMedia.bgFileName || "Preset"}\nSoundtrack: ${lastMedia.musicFileName || "Preset"}`
    : "";

  safeNotify(
    `🚀 Full Pipeline Triggered!\n` +
      `Rows prepared: ${preparedCount}\n` +
      `GitHub Actions is running metadata preparation and rendering your video shortly.${mediaInfo}`
  );
}

/**
 * Cycles deterministically through BG_FOLDER_ID and MUSIC_FOLDER_ID in Google Drive
 * using persistent Script Properties counters (LAST_BG_INDEX, LAST_MUSIC_INDEX).
 */
function getNextRotatedDriveMedia() {
  const props = PropertiesService.getScriptProperties();
  const bgFolderId = String(props.getProperty("BG_FOLDER_ID") || "").trim();
  const musicFolderId = String(props.getProperty("MUSIC_FOLDER_ID") || "").trim();

  const pickNextFromFolder = (folderId, counterKey, mimePrefix, validExts) => {
    if (!folderId) return { id: "", name: "", count: 0 };
    const folder = DriveApp.getFolderById(folderId);
    const iter = folder.getFiles();
    const items = [];

    while (iter.hasNext()) {
      const file = iter.next();
      if (file.isTrashed()) continue;
      const name = file.getName();
      const lower = name.toLowerCase();
      const mime = file.getMimeType() || "";
      const matchesMime = mime.indexOf(mimePrefix) === 0;
      const matchesExt = validExts.some((ext) => lower.endsWith(ext));
      if (matchesMime || matchesExt) {
        items.push({ id: file.getId(), name: name });
      }
    }

    if (items.length === 0) return { id: "", name: "", count: 0 };
    items.sort((a, b) => a.name.localeCompare(b.name));

    const lastIdx = parseInt(props.getProperty(counterKey) || "-1", 10);
    const nextIdx = (lastIdx + 1) % items.length;
    props.setProperty(counterKey, String(nextIdx));

    return { id: items[nextIdx].id, name: items[nextIdx].name, count: items.length };
  };

  const nextBg = pickNextFromFolder(
    bgFolderId,
    "LAST_BG_INDEX",
    "image/",
    [".jpg", ".jpeg", ".png", ".webp"]
  );
  const nextMusic = pickNextFromFolder(
    musicFolderId,
    "LAST_MUSIC_INDEX",
    "audio/",
    [".mp3", ".wav", ".m4a", ".aac", ".ogg"]
  );

  return {
    bgFileId: nextBg.id,
    bgFileName: nextBg.name,
    bgCount: nextBg.count,
    musicFileId: nextMusic.id,
    musicFileName: nextMusic.name,
    musicCount: nextMusic.count,
  };
}

function applyTemplateAndDriveMedia(sheet, targetRow) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const tplSheet = ss.getSheetByName("Templates");
  const props = PropertiesService.getScriptProperties();

  if (tplSheet && tplSheet.getLastRow() >= 2) {
    const tplLastRow = tplSheet.getLastRow();
    let nextTplRow = parseInt(props.getProperty("LAST_TEMPLATE_ROW") || "2", 10);
    if (nextTplRow < 2 || nextTplRow > tplLastRow) nextTplRow = 2;

    const tplValues = tplSheet.getRange(nextTplRow, 11, 1, 6).getValues()[0];
    for (let offset = 0; offset < 6; offset++) {
      const cell = sheet.getRange(targetRow, 11 + offset);
      if (!cell.getValue() && tplValues[offset]) {
        cell.setValue(tplValues[offset]);
      }
    }

    const followingTplRow = nextTplRow + 1 > tplLastRow ? 2 : nextTplRow + 1;
    props.setProperty("LAST_TEMPLATE_ROW", String(followingTplRow));
  }

  const themeCell = sheet.getRange(targetRow, 13);
  if (!themeCell.getValue()) themeCell.setValue("navy_gold");

  const voiceCell = sheet.getRange(targetRow, 14);
  if (!voiceCell.getValue()) voiceCell.setValue("Luke");

  const musicCell = sheet.getRange(targetRow, 15);
  const bgCell = sheet.getRange(targetRow, 16);
  let media = null;

  if (!musicCell.getValue() || !bgCell.getValue()) {
    media = getNextRotatedDriveMedia();
    if (!musicCell.getValue() && media.musicFileId) musicCell.setValue(media.musicFileId);
    if (!bgCell.getValue() && media.bgFileId) bgCell.setValue(media.bgFileId);
  }

  return media;
}

/**
 * Menu Step 1: Fetch new studies from NotebookLM.
 */
function manualFetchFromNotebook() {
  dispatchGitHubEvent("fetch_notebook_studies", false);
}

/**
 * Menu Step 2: Populate Creative Brief, Slide Breakdown, Visual Theme, Voice, Music & Background.
 */
function manualPrepareMetadata() {
  dispatchGitHubEvent("prepare_study_metadata", false);
}

/**
 * Menu Step 3: Trigger Video Render Only (renders the next Ready video as Unlisted).
 */
function manualRunCompletePipeline() {
  dispatchGitHubEvent("build_daily_video", false);
}

/**
 * Menu Step 4: Trigger approval_manager.py (`notify`) to email links for any rows in 'Review'
 * and advance them to 'Pending Review'.
 */
function manualSendApprovalEmails() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Sheet1");
  const data = sheet.getDataRange().getValues();
  let reviewCount = 0;

  for (let i = 1; i < data.length; i++) {
    const status = String(data[i][6] || "").trim().toLowerCase();
    if (status ===
