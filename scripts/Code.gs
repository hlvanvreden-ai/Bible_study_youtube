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
    .addItem("3. Prepare Next Study & Render Unlisted Video", "manualRunCompletePipeline")
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
 * Menu Step 3: Prepare the next Ready/Not Started row and render/upload it as Unlisted.
 */
function manualRunCompletePipeline() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Sheet1");
  const data = sheet.getDataRange().getValues();
  const sastDate = formatSAST(new Date(Date.now() + 30 * 60 * 1000));

  for (let i = 1; i < data.length; i++) {
    const title = String(data[i][1] || "").trim();
    const status = String(data[i][6] || "").trim();
    if (!title) continue;

    if (status === "" || status === "Not Started" || status === "Ready") {
      const rowNum = i + 1;
      sheet.getRange(rowNum, 7).setValue("Ready");
      if (!data[i][7]) sheet.getRange(rowNum, 8).setValue(sastDate);

      const media = applyTemplateAndDriveMedia(sheet, rowNum);
      dispatchGitHubEvent("prepare_study_metadata", true);
      dispatchGitHubEvent("build_daily_video", true);

      const mediaSummary = media
        ? `\nBackground: ${media.bgFileName || "Preset"}\nSoundtrack: ${media.musicFileName || "Preset"}`
        : "";
      safeNotify(
        `Row ${rowNum} (${title}) prepared & metadata/render workflow triggered!${mediaSummary}`
      );
      return;
    }
  }

  safeNotify("No 'Not Started' or 'Ready' studies found to process.");
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
    if (status === "review") {
      reviewCount++;
    }
  }

  if (reviewCount === 0) {
    safeNotify("No rows are currently marked 'Review' in Column G.");
    return;
  }

  dispatchGitHubEvent("send_approval_notifications", false);
}

/**
 * Menu Step 5: Trigger Stage 2 in generate_and_publish.py to schedule 'Approved' videos on YouTube.
 */
function manualScheduleApprovedVideos() {
  dispatchGitHubEvent("build_daily_video", false);
}

function triggerGitHubAction(silent = false) {
  dispatchGitHubEvent("build_daily_video", silent);
}

function dispatchGitHubEvent(eventType, silent = false) {
  const GITHUB_PAT = PropertiesService.getScriptProperties().getProperty("GITHUB_PAT");
  if (!GITHUB_PAT) {
    if (!silent) safeNotify("Error: GITHUB_PAT is missing in Project Settings > Script Properties.");
    throw new Error("GITHUB_PAT is missing in Script Properties.");
  }

  const url = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/dispatches`;
  const options = {
    method: "post",
    contentType: "application/json",
    headers: {
      Authorization: `Bearer ${GITHUB_PAT}`,
      Accept: "application/vnd.github.v3+json",
    },
    payload: JSON.stringify({ event_type: eventType }),
  };

  try {
    UrlFetchApp.fetch(url, options);
    if (!silent) safeNotify(`GitHub Action (${eventType}) triggered successfully!`);
  } catch (err) {
    if (!silent) safeNotify("Error triggering GitHub: " + err.message);
    throw err;
  }
}

function fillMissingReadyDates() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Sheet1");
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return;

  const sastDate = formatSAST(new Date(Date.now() + 30 * 60 * 1000));
  let updatedCount = 0;
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
      updatedCount++;
    }
  }

  if (lastMedia) {
    safeNotify(
      `Updated ${updatedCount} Ready row(s).\n` +
        `Drive Backgrounds found: ${lastMedia.bgCount} (Selected: ${lastMedia.bgFileName || "NONE"})\n` +
        `Drive Soundtracks found: ${lastMedia.musicCount} (Selected: ${lastMedia.musicFileName || "NONE"})`
    );
  } else {
    safeNotify("All Ready rows already have dates, templates, and Drive media assigned.");
  }
}

function resetTemplateCounter() {
  const props = PropertiesService.getScriptProperties();
  props.setProperty("LAST_TEMPLATE_ROW", "2");
  props.setProperty("LAST_BG_INDEX", "-1");
  props.setProperty("LAST_MUSIC_INDEX", "-1");
  safeNotify("Template row rotation reset to Row 2, and Drive image/soundtrack counters reset.");
}

function setupAutomaticTriggers() {
  const triggers = ScriptApp.getProjectTriggers();
  for (let i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "runFullPipelineOneGo") {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }

  ScriptApp.newTrigger("runFullPipelineOneGo").timeBased().everyHours(6).create();
  Logger.log("Automated pipeline trigger successfully created for runFullPipelineOneGo.");
}

function removeAutomaticTriggers() {
  const triggers = ScriptApp.getProjectTriggers();
  for (let i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === "runFullPipelineOneGo") {
      ScriptApp.deleteTrigger(triggers[i]);
    }
  }
  Logger.log("All pipeline triggers removed.");
}

function onEdit(e) {
  if (!e || !e.range) return;
  const sheet = e.range.getSheet();
  if (sheet.getName() !== "Sheet1") return;

  const startRow = e.range.getRow();
  const numRows = e.range.getNumRows();
  const startCol = e.range.getColumn();
  const endCol = startCol + e.range.getNumColumns() - 1;
  if (startRow < 2 || startCol > 2 || endCol < 1) return;

  for (let r = startRow; r < startRow + numRows; r++) {
    const idVal = sheet.getRange(r, 1).getValue();
    const titleVal = sheet.getRange(r, 2).getValue();
    if (!idVal && !titleVal) continue;

    const statusCell = sheet.getRange(r, 7);
    const dateCell = sheet.getRange(r, 8);
    if (statusCell.getValue() !== "" || dateCell.getValue() !== "") continue;

    const sameDayPublish = new Date(Date.now() + 30 * 60 * 1000);
    statusCell.setValue("Ready");
    dateCell.setValue(formatSAST(sameDayPublish));
    applyTemplateAndDriveMedia(sheet, r);
  }
}

function doGet(e) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Sheet1");
  const data = sheet.getDataRange().getValues();
  const pendingItems = [];

  for (let i = 1; i < data.length; i++) {
    const status = String(data[i][6] || "").trim().toLowerCase();
    if (status === "pending review") {
      const rawDate = data[i][7];
      const scheduledDate = rawDate instanceof Date ? formatSAST(rawDate) : String(rawDate);
      const ytUrl = String(data[i][9] || "").trim();
      const videoId = ytUrl.split("/").pop().split("?")[0];

      pendingItems.push({
        row: i + 1,
        title: String(data[i][1] || "Untitled Study"),
        category: String(data[i][2] || "General"),
        scripture: String(data[i][3] || ""),
        scheduledDate: scheduledDate,
        videoId: videoId,
        ytUrl: ytUrl,
      });
    }
  }

  pendingItems.reverse();
  const template = HtmlService.createTemplateFromFile("ReviewPage");
  template.pendingItems = pendingItems;

  return template
    .evaluate()
    .setTitle("Bible Study Video Approvals (" + pendingItems.length + ")")
    .addMetaTag("viewport", "width=device-width, initial-scale=1");
}

function processReviewDecision(row, decision, feedback) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Sheet1");
  if (decision === "approve") {
    sheet.getRange(row, 7).setValue("Approved");
    triggerGitHubAction(true);
    return "✅ Approved! GitHub Actions is now scheduling Row " + row + " on YouTube.";
  } else {
    sheet.getRange(row, 7).setValue("Rejected");
    if (feedback) {
      const currentBrief = String(sheet.getRange(row, 11).getValue() || "");
      sheet.getRange(row, 11).setValue(currentBrief + " | Revision: " + feedback);
    }
    return "❌ Rejected. Revision notes saved to Column K.";
  }
}
