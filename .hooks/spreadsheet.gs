/**
 * AI ログ受信スクリプト
 *
 * 【デプロイ手順】
 * 1. Google スプレッドシートを新規作成
 * 2. 拡張機能 > Apps Script を開く
 * 3. このファイルの内容を貼り付けて保存
 * 4. デプロイ > 新しいデプロイ
 *    - 種類: ウェブアプリ
 *    - 実行ユーザー: 自分
 *    - アクセス: 全員（匿名を含む）
 * 5. 発行されたURLを .hooks/config.json の spreadsheet_webhook_url に設定
 */

const SHEET_NAME = 'AIログ';

function doPost(e) {
  try {
    const payload = JSON.parse(e.postData.contents);
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sheet = ss.getSheetByName(SHEET_NAME);

    if (!sheet) {
      sheet = ss.insertSheet(SHEET_NAME);
      const headers = [
        '日時', 'ユーザー名', 'メール',
        'ツール', 'セッション名',
        'プロンプト', 'AI応答'
      ];
      sheet.appendRow(headers);
      sheet.setFrozenRows(1);

      // ヘッダー行のスタイル設定
      const headerRange = sheet.getRange(1, 1, 1, headers.length);
      headerRange.setBackground('#4a86e8');
      headerRange.setFontColor('#ffffff');
      headerRange.setFontWeight('bold');

      // 列幅設定（プロンプト・AI応答列を広げる）
      sheet.setColumnWidth(6, 400); // プロンプト
      sheet.setColumnWidth(7, 400); // AI応答
    }

    for (const row of payload.rows) {
      sheet.appendRow([
        row.timestamp,
        row.user_name,
        row.user_email,
        row.tool,
        row.session_title,
        row.user_prompt,
        row.ai_response,
      ]);
    }

    return ContentService
      .createTextOutput(JSON.stringify({ status: 'ok', count: payload.rows.length }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ status: 'error', message: err.toString() }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
