// Shared state, transport, formatting, and realtime synchronization primitives.
const DEFAULT_USER_AVATAR = "/default-user-avatar.webp?v=20260818";
const state = {
  tasks: [], sessions: [], skills: [], providers: [], memories: [], generatedMemories: [], commands: [], sshHosts: [], sshConfig: "", activeSSHHost: localStorage.getItem("codex-dashboard-ssh-host") || "",
  selectedId: null, selectedTask: null, selectedEvents: [], selectedMessages: [], pendingApprovals: [], sessionRequestId: 0, sessionAbortController: null, workspacePath: "", workspaceFile: null, workspaceRequestId: 0, workspacePickerPath: "", workspaceUploading: false, inspectorClosed: false, filter: "all", query: "",
  historyCursor: "", historyHasMore: false, historyLoading: false, chatBlocks: [], chatVirtualStart: null, chatAverageHeight: 112, activityVisibleCounts: {}, activityOutputOpen: {},
  commandIndex: 0, rawActivity: true, composerHistory: [], historyIndex: -1, editingQueuedId: null, defaultWorkspace: "", codexAvailable: true, codexInstall: null, codexInfo: null,
  runtimeMetrics: { taskId: "", ttftMs: null, tpotMs: null, estimated: true, outputTokens: 0 }, serverInfo: { user: "", hostname: "" }, chatSnapToBottom: false, chatSelectionActive: false, chatPointerSelecting: false, chatPointerDragged: false, chatPointerStartX: 0, chatPointerStartY: 0, userAvatar: localStorage.getItem("codex-dashboard-user-avatar") || DEFAULT_USER_AVATAR,
};
const legacyUserAvatar = localStorage.getItem("codex-dashboard-user-avatar") || "";
state.selectedId = localStorage.getItem("codex-dashboard-session") || null;
state.language = localStorage.getItem("codex-dashboard-language") || "zh";
state.titleExpanded = false;
const LANGUAGE_PACKS = {
  zh: { newSession: "新会话", terminal: "终端", nextSession: "下一个会话", previousSession: "上一个会话", firstSession: "首个会话", searchSessions: "搜索会话", workspace: "工作区", sessions: "会话", memories: "记忆", skills: "技能", all: "全部", running: "进行中", available: "待处理", trash: "回收站", sync: "同步本机 Codex", emptyTitle: "开始一个 Codex 会话", emptyCopy: "从左侧选择已有线程，或新建一个会话开始工作。", create: "新建会话", rename: "重命名", fork: "复制会话", more: "更多操作", send: "发送", start: "开始", pause: "暂停", expand: "展开", collapse: "收起", statusRunning: "运行中", statusRetrying: "重试中", statusQueued: "排队中", statusAvailable: "待处理", statusSucceeded: "已完成", statusFailed: "失败", statusStopped: "已停止", statusArchived: "已归档", statusTrashed: "回收站", statusImported: "已导入", unknown: "未知", composerPlaceholder: "给 Codex 发送消息…" },
  en: { newSession: "New session", terminal: "Terminal", nextSession: "Next session", previousSession: "Previous session", firstSession: "First session", searchSessions: "Search sessions", workspace: "Workspace", sessions: "Sessions", memories: "Memory", skills: "Skills", all: "All", running: "Running", available: "Pending", trash: "Trash", sync: "Sync Codex", emptyTitle: "Start a Codex session", emptyCopy: "Choose a thread on the left, or create a new session.", create: "New session", rename: "Rename", fork: "Duplicate", more: "More actions", send: "Send", start: "Start", pause: "Pause", expand: "Expand", collapse: "Collapse", statusRunning: "Running", statusRetrying: "Retrying", statusQueued: "Queued", statusAvailable: "Pending", statusSucceeded: "Completed", statusFailed: "Failed", statusStopped: "Stopped", statusArchived: "Archived", statusTrashed: "Trash", statusImported: "Imported", unknown: "Unknown", composerPlaceholder: "Message Codex…" },
  fr: { newSession: "Nouvelle session", terminal: "Terminal", workspace: "Espace de travail", sessions: "Sessions", memories: "Mémoire", skills: "Skills", all: "Toutes", running: "En cours", available: "En attente", trash: "Corbeille", sync: "Synchroniser Codex", emptyTitle: "Démarrer une session Codex", emptyCopy: "Choisissez un fil à gauche ou créez une session.", create: "Nouvelle session", rename: "Renommer", fork: "Dupliquer", more: "Plus d'actions", send: "Envoyer", expand: "Développer", collapse: "Réduire", statusRunning: "En cours", statusRetrying: "Nouvel essai", statusQueued: "En file", statusAvailable: "En attente", statusSucceeded: "Terminé", statusFailed: "Échec", statusStopped: "Arrêté", statusArchived: "Archivé", statusTrashed: "Corbeille", statusImported: "Importé", unknown: "Inconnu", composerPlaceholder: "Écrire à Codex…" },
  ja: { newSession: "新しいセッション", terminal: "ターミナル", workspace: "ワークスペース", sessions: "セッション", memories: "メモリ", skills: "Skills", all: "すべて", running: "実行中", available: "保留中", trash: "ゴミ箱", sync: "Codexを同期", emptyTitle: "Codexセッションを開始", emptyCopy: "左からスレッドを選ぶか、新しいセッションを作成してください。", create: "新しいセッション", rename: "名前を変更", fork: "複製", more: "その他", send: "送信", expand: "展開", collapse: "折りたたむ", statusRunning: "実行中", statusRetrying: "再試行中", statusQueued: "待機中", statusAvailable: "保留中", statusSucceeded: "完了", statusFailed: "失敗", statusStopped: "停止", statusArchived: "アーカイブ済み", statusTrashed: "ゴミ箱", statusImported: "インポート済み", unknown: "不明", composerPlaceholder: "Codexにメッセージ…" },
  ko: { newSession: "새 세션", terminal: "터미널", workspace: "작업 공간", sessions: "세션", memories: "메모리", skills: "Skills", all: "전체", running: "실행 중", available: "대기 중", trash: "휴지통", sync: "Codex 동기화", emptyTitle: "Codex 세션 시작", emptyCopy: "왼쪽에서 스레드를 선택하거나 새 세션을 만드세요.", create: "새 세션", rename: "이름 변경", fork: "복제", more: "더 보기", send: "보내기", expand: "펼치기", collapse: "접기", statusRunning: "실행 중", statusRetrying: "재시도 중", statusQueued: "대기열", statusAvailable: "대기 중", statusSucceeded: "완료", statusFailed: "실패", statusStopped: "중지됨", statusArchived: "보관됨", statusTrashed: "휴지통", statusImported: "가져옴", unknown: "알 수 없음", composerPlaceholder: "Codex에 메시지…" },
};
const LANGUAGE_EXTRAS = {
  fr: { nextSession: "Session suivante", previousSession: "Session précédente", firstSession: "Première session", searchSessions: "Rechercher des sessions", skills: "Compétences" },
  ja: { nextSession: "次のセッション", previousSession: "前のセッション", firstSession: "最初のセッション", searchSessions: "セッションを検索", skills: "スキル" },
  ko: { nextSession: "다음 세션", previousSession: "이전 세션", firstSession: "첫 세션", searchSessions: "세션 검색", skills: "스킬" },
};
function t(key, fallback = key, options = {}) {
  if (window.i18next?.isInitialized) return window.i18next.t(key, { defaultValue: fallback, ...options });
  return LANGUAGE_EXTRAS[state.language]?.[key] || LANGUAGE_PACKS[state.language]?.[key] || LANGUAGE_PACKS.zh[key] || fallback;
}
const UI_LABELS = {
  local: { zh: "本机", en: "Local", fr: "Local", ja: "ローカル", ko: "로컬" },
  emptySession: { zh: "空会话", en: "Empty session", fr: "Session vide", ja: "空のセッション", ko: "빈 세션" },
  controlled: { zh: "受控", en: "Controlled", fr: "Contrôlé", ja: "制御", ko: "제어됨" },
  activity: { zh: "工具与活动", en: "Tools and activity", fr: "Outils et activité", ja: "ツールとアクティビティ", ko: "도구 및 활동" },
  activityCurrent: { zh: "当前正在执行", en: "Running now", fr: "En cours", ja: "実行中", ko: "현재 실행 중" },
  activityWorking: { zh: "正在处理", en: "Working", fr: "Traitement", ja: "処理中", ko: "처리 중" },
  activityPlanning: { zh: "正在分析并规划", en: "Analyzing and planning", fr: "Analyse et planification", ja: "分析と計画中", ko: "분석 및 계획 중" },
  activityCommand: { zh: "正在运行命令", en: "Running command", fr: "Exécution de commande", ja: "コマンドを実行中", ko: "명령 실행 중" },
  activityCommandDone: { zh: "命令执行完成", en: "Command completed", fr: "Commande terminée", ja: "コマンド完了", ko: "명령 완료" },
  activityFile: { zh: "正在修改文件", en: "Updating files", fr: "Modification des fichiers", ja: "ファイルを更新中", ko: "파일 수정 중" },
  activityFileDone: { zh: "文件修改完成", en: "Files updated", fr: "Fichiers modifiés", ja: "ファイル更新完了", ko: "파일 수정 완료" },
  activityTool: { zh: "正在调用工具", en: "Using tool", fr: "Utilisation d’un outil", ja: "ツールを使用中", ko: "도구 사용 중" },
  activityToolDone: { zh: "工具调用完成", en: "Tool completed", fr: "Outil terminé", ja: "ツール完了", ko: "도구 호출 완료" },
  activitySearch: { zh: "正在检索资料", en: "Searching", fr: "Recherche", ja: "検索中", ko: "검색 중" },
  loadEarlierActivity: { zh: "查看较早活动（{{count}}）", en: "Load earlier activity ({{count}})", fr: "Charger les activités précédentes ({{count}})", ja: "以前のアクティビティを表示（{{count}}）", ko: "이전 활동 보기 ({{count}})" },
  sending: { zh: "发送中", en: "Sending", fr: "Envoi", ja: "送信中", ko: "전송 중" },
  steering: { zh: "当前 turn", en: "Current turn", fr: "Tour actuel", ja: "現在のターン", ko: "현재 턴" },
  queued: { zh: "排队中", en: "Queued", fr: "En file", ja: "待機中", ko: "대기 중" },
  failedSend: { zh: "发送失败", en: "Send failed", fr: "Échec de l'envoi", ja: "送信失敗", ko: "전송 실패" },
  firstMessage: { zh: "发送第一条消息，开始这个 Codex 会话。", en: "Send the first message to start this Codex session.", fr: "Envoyez un premier message pour démarrer cette session Codex.", ja: "最初のメッセージを送ってCodexセッションを開始します。", ko: "첫 메시지를 보내 Codex 세션을 시작하세요." },
  openSessions: { zh: "打开会话列表", en: "Open sessions", fr: "Ouvrir les sessions", ja: "セッション一覧を開く", ko: "세션 목록 열기" },
  closeSessions: { zh: "关闭会话列表", en: "Close sessions", fr: "Fermer les sessions", ja: "セッション一覧を閉じる", ko: "세션 목록 닫기" },
  wastelandTheme: { zh: "切换废土终端主题", en: "Switch to wasteland terminal theme", fr: "Activer le thème terminal rétro", ja: "ウェイストランド端末テーマに切替", ko: "황무지 터미널 테마로 전환" },
  darkTheme: { zh: "切换默认深色主题", en: "Switch to default dark theme", fr: "Revenir au thème sombre", ja: "標準ダークテーマに戻す", ko: "기본 다크 테마로 전환" },
  wastelandEnabled: { zh: "已进入废土终端模式", en: "Wasteland terminal enabled", fr: "Terminal rétro activé", ja: "ウェイストランド端末を有効化しました", ko: "황무지 터미널 모드 활성화" },
  darkEnabled: { zh: "已恢复默认深色主题", en: "Default dark theme restored", fr: "Thème sombre restauré", ja: "標準ダークテーマに戻しました", ko: "기본 다크 테마 복원" },
  mascotDance: { zh: "Codex Partner 与废土侦察机正在表演未来双人舞", en: "Codex Partner and the wasteland scout are performing a future duet", fr: "Codex Partner et l’éclaireur présentent un duo futuriste", ja: "Codex Partner と偵察ユニットが未来的なデュエットをパフォーマンス中", ko: "Codex Partner와 황무지 정찰 유닛이 미래적인 듀엣 공연 중" },
  loadOlderMessages: { zh: "查看更早消息（{{count}}）", en: "Load earlier messages ({{count}})", fr: "Charger les messages précédents ({{count}})", ja: "以前のメッセージを表示（{{count}}）", ko: "이전 메시지 보기 ({{count}})" },
  loadEarlierMessages: { zh: "查看更早消息", en: "Load earlier messages", fr: "Charger les messages précédents", ja: "以前のメッセージを表示", ko: "이전 메시지 보기" },
  localThread: { zh: "本机 Codex 线程", en: "Local Codex thread", fr: "Fil Codex local", ja: "ローカルCodexスレッド", ko: "로컬 Codex 스레드" },
  threadId: { zh: "线程", en: "Thread", fr: "Fil", ja: "スレッド", ko: "스레드" },
  sshSession: { zh: "SSH Codex 会话", en: "SSH Codex session", fr: "Session Codex SSH", ja: "SSH Codexセッション", ko: "SSH Codex 세션" },
  webSession: { zh: "网页会话", en: "Web session", fr: "Session web", ja: "ウェブセッション", ko: "웹 세션" },
  terminalLabel: { zh: "终端", en: "Terminal", fr: "Terminal", ja: "ターミナル", ko: "터미널" },
  commandLabel: { zh: "命令", en: "Command", fr: "Commande", ja: "コマンド", ko: "명령" },
  commandResult: { zh: "命令结果", en: "Command result", fr: "Résultat", ja: "コマンド結果", ko: "명령 결과" },
  model: { zh: "模型", en: "Model", fr: "Modèle", ja: "モデル", ko: "모델" },
  effort: { zh: "思考", en: "Reasoning", fr: "Raisonnement", ja: "思考", ko: "추론" },
  providerDefault: { zh: "Provider 默认", en: "Provider default", fr: "Par défaut", ja: "Provider既定", ko: "Provider 기본" },
  providerDefaultModel: { zh: "Provider 默认模型", en: "Provider default model", fr: "Modèle par défaut", ja: "Provider既定モデル", ko: "Provider 기본 모델" },
  minimal: { zh: "最小", en: "Minimal", fr: "Minimal", ja: "最小", ko: "최소" },
  low: { zh: "低", en: "Low", fr: "Faible", ja: "低", ko: "낮음" },
  medium: { zh: "中", en: "Medium", fr: "Moyen", ja: "中", ko: "중간" },
  high: { zh: "高", en: "High", fr: "Élevé", ja: "高", ko: "높음" },
  xhigh: { zh: "很高", en: "Very high", fr: "Très élevé", ja: "非常に高い", ko: "매우 높음" },
  ultra: { zh: "极高", en: "Ultra", fr: "Très élevé+", ja: "極高", ko: "최고" },
  you: { zh: "用户", en: "User", fr: "Utilisateur", ja: "ユーザー", ko: "사용자" },
  start: { zh: "开始", en: "Start", fr: "Démarrer", ja: "開始", ko: "시작" },
  pause: { zh: "暂停", en: "Pause", fr: "Pause", ja: "一時停止", ko: "일시정지" },
  goalStart: { zh: "开始目标续跑", en: "Start goal resume", fr: "Reprendre l’objectif", ja: "目標の再開", ko: "목표 재개" },
  goalPause: { zh: "暂停目标续跑", en: "Pause goal resume", fr: "Suspendre l’objectif", ja: "目標を一時停止", ko: "목표 일시정지" },
  setGoal: { zh: "先设置目标", en: "Set a goal first", fr: "Définissez un objectif", ja: "目標を設定してください", ko: "목표를 먼저 설정하세요" },
  retryOn: { zh: "开启目标自动续跑", en: "Enable goal auto-resume", fr: "Activer la reprise automatique", ja: "目標の自動再開を有効化", ko: "목표 자동 재개 켜기" },
  retryOff: { zh: "关闭目标自动续跑", en: "Disable goal auto-resume", fr: "Désactiver la reprise automatique", ja: "目標の自動再開を無効化", ko: "목표 자동 재개 끄기" },
  on: { zh: "开", en: "On", fr: "Oui", ja: "オン", ko: "켬" },
  off: { zh: "关", en: "Off", fr: "Non", ja: "オフ", ko: "끔" },
  save: { zh: "保存", en: "Save", fr: "Enregistrer", ja: "保存", ko: "저장" },
  saveQueued: { zh: "保存排队消息（Enter）", en: "Save queued message (Enter)", fr: "Enregistrer le message en attente (Entrée)", ja: "待機メッセージを保存 (Enter)", ko: "대기 메시지 저장 (Enter)" },
  pauseCodex: { zh: "暂停当前 Codex turn（Enter）", en: "Pause the current Codex turn (Enter)", fr: "Mettre en pause le turn Codex (Entrée)", ja: "現在のCodex turnを一時停止 (Enter)", ko: "현재 Codex turn 일시정지 (Enter)" },
  startCodex: { zh: "开始 Codex（Enter）", en: "Start Codex (Enter)", fr: "Démarrer Codex (Entrée)", ja: "Codexを開始 (Enter)", ko: "Codex 시작 (Enter)" },
  realtime: { zh: "实时连接", en: "Live", fr: "Temps réel", ja: "リアルタイム", ko: "실시간" },
  disconnected: { zh: "连接中断", en: "Disconnected", fr: "Déconnecté", ja: "切断", ko: "연결 끊김" },
  reconnecting: { zh: "重连中", en: "Reconnecting", fr: "Reconnexion", ja: "再接続中", ko: "재연결 중" },
  connecting: { zh: "连接中", en: "Connecting", fr: "Connexion", ja: "接続中", ko: "연결 중" },
  currentApi: { zh: "当前 API", en: "Current API", fr: "API actuelle", ja: "現在の API", ko: "현재 API" },
  automatic: { zh: "自动选择", en: "Auto", fr: "Auto", ja: "自動選択", ko: "자동 선택" },
  estimated: { zh: "估算", en: "estimated", fr: "estimé", ja: "推定", ko: "추정" },
  session: { zh: "会话", en: "Session", fr: "Session", ja: "セッション", ko: "세션" },
  state: { zh: "状态", en: "Status", fr: "État", ja: "状態", ko: "상태" },
  thread: { zh: "线程", en: "Thread", fr: "Thread", ja: "スレッド", ko: "스레드" },
  sshHost: { zh: "SSH 主机", en: "SSH host", fr: "Hôte SSH", ja: "SSHホスト", ko: "SSH 호스트" },
  workspacePath: { zh: "工作目录", en: "Workspace", fr: "Espace de travail", ja: "ワークスペース", ko: "작업 공간" },
  goal: { zh: "目标", en: "Goal", fr: "Objectif", ja: "目標", ko: "목표" },
  goalStatusPrefix: { zh: "目标", en: "Goal", fr: "Objectif", ja: "目標", ko: "목표" },
  noGoal: { zh: "没有设置目标", en: "No goal set", fr: "Aucun objectif défini", ja: "目標未設定", ko: "설정된 목표 없음" },
  progress: { zh: "进度", en: "Progress", fr: "Progression", ja: "進捗", ko: "진행률" },
  runHistory: { zh: "运行记录", en: "Run history", fr: "Historique", ja: "実行履歴", ko: "실행 기록" },
  attempt: { zh: "尝试", en: "Attempt", fr: "Tentative", ja: "試行", ko: "시도" },
  autoProvider: { zh: "自动 Provider", en: "Auto Provider", fr: "Provider auto", ja: "自動 Provider", ko: "자동 Provider" },
  noRunHistory: { zh: "暂无运行记录", en: "No runs yet", fr: "Aucune exécution", ja: "実行履歴なし", ko: "실행 기록 없음" },
  threadControls: { zh: "线程控制", en: "Thread controls", fr: "Contrôles du thread", ja: "スレッド操作", ko: "스레드 제어" },
  copySession: { zh: "复制会话", en: "Duplicate session", fr: "Dupliquer", ja: "セッションを複製", ko: "세션 복제" },
  compactContext: { zh: "压缩上下文", en: "Compact context", fr: "Compacter le contexte", ja: "コンテキストを圧縮", ko: "컨텍스트 압축" },
  archive: { zh: "归档", en: "Archive", fr: "Archiver", ja: "アーカイブ", ko: "보관" },
  unarchive: { zh: "取消归档", en: "Unarchive", fr: "Désarchiver", ja: "アーカイブ解除", ko: "보관 해제" },
  moveTrash: { zh: "移到回收站", en: "Move to trash", fr: "Mettre à la corbeille", ja: "ゴミ箱へ移動", ko: "휴지통으로 이동" },
  enableMemory: { zh: "打开记忆", en: "Enable memory", fr: "Activer la mémoire", ja: "メモリを有効化", ko: "메모리 켜기" },
  disableMemory: { zh: "关闭记忆", en: "Disable memory", fr: "Désactiver la mémoire", ja: "メモリを無効化", ko: "메모리 끄기" },
  workspaceFiles: { zh: "工作区文件", en: "Workspace files", fr: "Fichiers de l’espace", ja: "ワークスペースのファイル", ko: "작업 공간 파일" },
  workspaceRoot: { zh: "工作区根目录", en: "Workspace root", fr: "Racine de l'espace", ja: "ワークスペースのルート", ko: "작업 공간 루트" },
  chooseWorkspace: { zh: "选择工作目录", en: "Choose workspace", fr: "Choisir l'espace", ja: "ワークスペースを選択", ko: "작업 공간 선택" },
  back: { zh: "返回上级", en: "Back", fr: "Retour", ja: "上へ戻る", ko: "상위로" },
  changeDirectory: { zh: "更改目录", en: "Change directory", fr: "Changer de dossier", ja: "ディレクトリを変更", ko: "디렉터리 변경" },
  upload: { zh: "上传", en: "Upload", fr: "Téléverser", ja: "アップロード", ko: "업로드" },
  edit: { zh: "编辑", en: "Edit", fr: "Modifier", ja: "編集", ko: "편집" },
  download: { zh: "下载", en: "Download", fr: "Télécharger", ja: "ダウンロード", ko: "다운로드" },
  emptyDirectory: { zh: "目录为空，可将文件拖到这里上传。", en: "Directory is empty. Drop files here to upload.", fr: "Dossier vide. Déposez des fichiers pour téléverser.", ja: "空のディレクトリです。ファイルをドロップしてアップロードできます。", ko: "디렉터리가 비어 있습니다. 파일을 놓아 업로드하세요." },
  language: { zh: "界面语言", en: "Interface language", fr: "Langue", ja: "表示言語", ko: "인터페이스 언어" },
  clear: { zh: "清空", en: "Clear", fr: "Effacer", ja: "クリア", ko: "지우기" },
  cancel: { zh: "取消", en: "Cancel", fr: "Annuler", ja: "キャンセル", ko: "취소" },
  save: { zh: "保存", en: "Save", fr: "Enregistrer", ja: "保存", ko: "저장" },
  editGoal: { zh: "修改目标", en: "Edit goal", fr: "Modifier l’objectif", ja: "目標を編集", ko: "목표 수정" },
  goalInput: { zh: "输入要持续完成的目标", en: "Enter the goal to pursue", fr: "Saisissez l’objectif", ja: "継続する目標を入力", ko: "추적할 목표 입력" },
  ready: { zh: "待命", en: "Ready", fr: "Prêt", ja: "待機中", ko: "대기" },
  shortcutTerminal: { zh: "单按反引号键 (`) 显示/隐藏终端", en: "Press the backquote key (`) to toggle terminal", fr: "Touche accent grave (`) : afficher/masquer le terminal", ja: "バッククォート (`) でターミナル表示切替", ko: "백쿼트 (`) 키로 터미널 표시 전환" },
  shortcutNext: { zh: "下个会话", en: "Next", fr: "Suivante", ja: "次へ", ko: "다음" },
  shortcutPrevious: { zh: "上个会话", en: "Previous", fr: "Précédente", ja: "前へ", ko: "이전" },
  shortcutFirst: { zh: "首个会话", en: "First", fr: "Première", ja: "先頭", ko: "처음" },
  shortcutComposer: { zh: "输入框", en: "Composer", fr: "Saisie", ja: "入力欄", ko: "입력창" },
  shortcutComposerToggle: { zh: "聚焦/取消聚焦输入框", en: "Focus or unfocus the composer", fr: "Activer ou quitter la zone de saisie", ja: "入力欄のフォーカスを切り替え", ko: "입력창 포커스 전환" },
  stopAction: { zh: "停止", en: "Stop", fr: "Arrêter", ja: "停止", ko: "중지" },
  editQueued: { zh: "修改排队中的消息…", en: "Edit queued message…", fr: "Modifier le message en attente…", ja: "待機メッセージを編集…", ko: "대기 메시지 편집…" },
  readingDirectory: { zh: "正在读取目录…", en: "Reading directory…", fr: "Lecture du dossier…", ja: "ディレクトリを読み込み中…", ko: "디렉터리 읽는 중…" },
  open: { zh: "打开", en: "Open", fr: "Ouvrir", ja: "開く", ko: "열기" },
  noSubdirectories: { zh: "没有子目录", en: "No subdirectories", fr: "Aucun sous-dossier", ja: "サブディレクトリなし", ko: "하위 디렉터리 없음" },
  go: { zh: "前往", en: "Go", fr: "Aller", ja: "移動", ko: "이동" },
  selectCurrentDirectory: { zh: "选择当前目录", en: "Select current directory", fr: "Sélectionner ce dossier", ja: "現在のディレクトリを選択", ko: "현재 디렉터리 선택" },
  serverDirectory: { zh: "服务器目录", en: "Server directory", fr: "Dossier serveur", ja: "サーバーディレクトリ", ko: "서버 디렉터리" },
  attachments: { zh: "附件", en: "Attachments", fr: "Pièces jointes", ja: "添付ファイル", ko: "첨부 파일" },
  filesReady: { zh: "{{count}} 个文件已加入下一条消息", en: "{{count}} files added to the next message", fr: "{{count}} fichiers ajoutés au prochain message", ja: "{{count}}個のファイルを次のメッセージに追加", ko: "{{count}}개 파일을 다음 메시지에 추가" },
  fileReadFailed: { zh: "文件读取失败：{{name}}", en: "Could not read file: {{name}}", fr: "Lecture impossible : {{name}}", ja: "ファイルを読み込めません：{{name}}", ko: "파일을 읽을 수 없음: {{name}}" },
  fileUploadFailed: { zh: "文件上传失败：{{name}}", en: "Could not upload file: {{name}}", fr: "Envoi impossible : {{name}}", ja: "ファイルをアップロードできません：{{name}}", ko: "파일을 업로드할 수 없음: {{name}}" },
  binaryAttachment: { zh: "已上传到工作区：{{path}}", en: "Uploaded to workspace: {{path}}", fr: "Téléversé dans l’espace : {{path}}", ja: "ワークスペースにアップロード済み：{{path}}", ko: "작업 공간에 업로드됨: {{path}}" },
  delete: { zh: "删除", en: "Delete", fr: "Supprimer", ja: "削除", ko: "삭제" },
  codexMemory: { zh: "Codex 记忆", en: "Codex memory", fr: "Mémoire Codex", ja: "Codex メモリ", ko: "Codex 메모리" },
  memorySummary: { zh: "{{markdown}} 个 Markdown 记忆 · {{generated}} 个生成记忆", en: "{{markdown}} Markdown memories · {{generated}} generated memories", fr: "{{markdown}} mémoires Markdown · {{generated}} mémoires générées", ja: "Markdown メモリ {{markdown}} 件 · 生成メモリ {{generated}} 件", ko: "Markdown 메모리 {{markdown}}개 · 생성된 메모리 {{generated}}개" },
  searchMemory: { zh: "搜索记忆", en: "Search memory", fr: "Rechercher dans la mémoire", ja: "メモリを検索", ko: "메모리 검색" },
  newItem: { zh: "新建", en: "New", fr: "Nouveau", ja: "新規", ko: "새로 만들기" },
  editable: { zh: "可编辑", en: "Editable", fr: "Modifiable", ja: "編集可能", ko: "편집 가능" },
  readOnly: { zh: "只读", en: "Read only", fr: "Lecture seule", ja: "読み取り専用", ko: "읽기 전용" },
  view: { zh: "查看", en: "View", fr: "Afficher", ja: "表示", ko: "보기" },
  generated: { zh: "生成记忆", en: "Generated", fr: "Générée", ja: "生成メモリ", ko: "생성된 메모리" },
  memoryUsage: { zh: "使用 {{count}} 次", en: "Used {{count}} times", fr: "Utilisée {{count}} fois", ja: "{{count}} 回使用", ko: "{{count}}회 사용" },
  noMarkdownMemories: { zh: "没有匹配的 Markdown 记忆", en: "No matching Markdown memories", fr: "Aucune mémoire Markdown correspondante", ja: "一致する Markdown メモリはありません", ko: "일치하는 Markdown 메모리가 없습니다" },
  noGeneratedMemories: { zh: "暂无生成记忆", en: "No generated memories", fr: "Aucune mémoire générée", ja: "生成メモリはありません", ko: "생성된 메모리가 없습니다" },
  rebuildGeneratedMemories: { zh: "重建生成记忆", en: "Rebuild generated memory", fr: "Reconstruire la mémoire générée", ja: "生成メモリを再構築", ko: "생성된 메모리 다시 만들기" },
  installedSkillsSummary: { zh: "已检测 {{count}} 个已安装 Skill，Codex 与 Agent Skill 均可直接编辑。", en: "{{count}} installed skills detected. Codex and Agent skills can be edited directly.", fr: "{{count}} skills installés détectés. Les skills Codex et Agent sont modifiables directement.", ja: "インストール済み Skill を {{count}} 件検出しました。Codex と Agent の Skill を直接編集できます。", ko: "설치된 Skill {{count}}개를 찾았습니다. Codex와 Agent Skill을 직접 편집할 수 있습니다." },
  newPersonalSkill: { zh: "新建个人 Skill", en: "New personal skill", fr: "Nouveau skill personnel", ja: "個人 Skill を作成", ko: "개인 Skill 만들기" },
  enabled: { zh: "已启用", en: "Enabled", fr: "Activé", ja: "有効", ko: "활성화됨" },
  disabled: { zh: "已停用", en: "Disabled", fr: "Désactivé", ja: "無効", ko: "비활성화됨" },
  enable: { zh: "启用", en: "Enable", fr: "Activer", ja: "有効にする", ko: "활성화" },
  noSkills: { zh: "未检测到 Skill", en: "No skills detected", fr: "Aucun skill détecté", ja: "Skill が見つかりません", ko: "Skill을 찾지 못했습니다" },
  editMemory: { zh: "编辑记忆", en: "Edit memory", fr: "Modifier la mémoire", ja: "メモリを編集", ko: "메모리 편집" },
  newMemory: { zh: "新建记忆", en: "New memory", fr: "Nouvelle mémoire", ja: "新しいメモリ", ko: "새 메모리" },
  createMarkdownMemory: { zh: "创建 Codex Markdown 记忆", en: "Create a Codex Markdown memory", fr: "Créer une mémoire Codex Markdown", ja: "Codex Markdown メモリを作成", ko: "Codex Markdown 메모리 만들기" },
  reading: { zh: "正在读取…", en: "Loading…", fr: "Chargement…", ja: "読み込み中…", ko: "불러오는 중…" },
  fileName: { zh: "文件名", en: "File name", fr: "Nom du fichier", ja: "ファイル名", ko: "파일 이름" },
  content: { zh: "内容", en: "Content", fr: "Contenu", ja: "内容", ko: "내용" },
  saving: { zh: "保存中…", en: "Saving…", fr: "Enregistrement…", ja: "保存中…", ko: "저장 중…" },
  memorySaved: { zh: "记忆已保存", en: "Memory saved", fr: "Mémoire enregistrée", ja: "メモリを保存しました", ko: "메모리가 저장되었습니다" },
  saveFailed: { zh: "保存失败：{{message}}", en: "Save failed: {{message}}", fr: "Échec de l’enregistrement : {{message}}", ja: "保存に失敗しました：{{message}}", ko: "저장 실패: {{message}}" },
  close: { zh: "关闭", en: "Close", fr: "Fermer", ja: "閉じる", ko: "닫기" },
  viewSkill: { zh: "查看 Skill", en: "View skill", fr: "Afficher le skill", ja: "Skill を表示", ko: "Skill 보기" },
  editSkill: { zh: "编辑 Skill", en: "Edit skill", fr: "Modifier le skill", ja: "Skill を編集", ko: "Skill 편집" },
  name: { zh: "名称", en: "Name", fr: "Nom", ja: "名前", ko: "이름" },
  description: { zh: "描述", en: "Description", fr: "Description", ja: "説明", ko: "설명" },
  skillSaved: { zh: "Skill 已保存", en: "Skill saved", fr: "Skill enregistré", ja: "Skill を保存しました", ko: "Skill이 저장되었습니다" },
  editFile: { zh: "编辑文件", en: "Edit file", fr: "Modifier le fichier", ja: "ファイルを編集", ko: "파일 편집" },
  fileSaved: { zh: "文件已保存", en: "File saved", fr: "Fichier enregistré", ja: "ファイルを保存しました", ko: "파일이 저장되었습니다" },
  unsupportedFileEdit: { zh: "该文件不支持网页编辑", en: "This file cannot be edited in the browser", fr: "Ce fichier ne peut pas être modifié dans le navigateur", ja: "このファイルはブラウザーで編集できません", ko: "이 파일은 브라우저에서 편집할 수 없습니다" },
  editorUnavailable: { zh: "文本编辑器未加载，请刷新页面重试", en: "The text editor did not load. Refresh and try again.", fr: "L’éditeur de texte n’est pas chargé. Actualisez la page.", ja: "テキストエディターを読み込めませんでした。ページを更新してください。", ko: "텍스트 편집기를 불러오지 못했습니다. 페이지를 새로 고치세요." },
  connectionAndProviders: { zh: "连接与 Provider", en: "Connection and providers", fr: "Connexion et providers", ja: "接続と Provider", ko: "연결 및 Provider" },
  connectionDescription: { zh: "当前网页服务、实时指标和 Provider 路由集中管理。", en: "Manage this web service, live metrics, and provider routing in one place.", fr: "Gérez le service web, les mesures en direct et le routage des providers.", ja: "Web サービス、リアルタイム指標、Provider ルーティングを一元管理します。", ko: "웹 서비스, 실시간 지표 및 Provider 라우팅을 한곳에서 관리합니다." },
  webService: { zh: "网页服务", en: "Web service", fr: "Service web", ja: "Web サービス", ko: "웹 서비스" },
  serviceUser: { zh: "服务用户", en: "Service user", fr: "Utilisateur du service", ja: "サービスユーザー", ko: "서비스 사용자" },
  liveConnected: { zh: "实时已连接", en: "Live connected", fr: "Temps réel connecté", ja: "リアルタイム接続済み", ko: "실시간 연결됨" },
  currentProvider: { zh: "当前 Provider", en: "Current provider", fr: "Provider actuel", ja: "現在の Provider", ko: "현재 Provider" },
  usableRoutes: { zh: "可用路由", en: "Available routes", fr: "Routes disponibles", ja: "利用可能なルート", ko: "사용 가능한 경로" },
  codexReady: { zh: "Codex 已就绪", en: "Codex ready", fr: "Codex prêt", ja: "Codex 準備完了", ko: "Codex 준비됨" },
  codexMissing: { zh: "Codex 未安装", en: "Codex not installed", fr: "Codex non installé", ja: "Codex 未インストール", ko: "Codex 설치되지 않음" },
  codexManagement: { zh: "Codex 管理", en: "Codex management", fr: "Gestion de Codex", ja: "Codex 管理", ko: "Codex 관리" },
  codexVersion: { zh: "版本", en: "Version", fr: "Version", ja: "バージョン", ko: "버전" },
  codexPath: { zh: "Codex 路径", en: "Codex path", fr: "Chemin Codex", ja: "Codex パス", ko: "Codex 경로" },
  codexWorkingDirectory: { zh: "当前路径", en: "Current path", fr: "Chemin actuel", ja: "現在のパス", ko: "현재 경로" },
  codexSource: { zh: "来源", en: "Source", fr: "Source", ja: "ソース", ko: "소스" },
  updateCodex: { zh: "更新 Codex", en: "Update Codex", fr: "Mettre Codex à jour", ja: "Codex を更新", ko: "Codex 업데이트" },
  updatingCodex: { zh: "更新中…", en: "Updating…", fr: "Mise à jour…", ja: "更新中…", ko: "업데이트 중…" },
  codexVersionUnknown: { zh: "版本未知", en: "Version unavailable", fr: "Version indisponible", ja: "バージョン不明", ko: "버전 확인 불가" },
  runningTasks: { zh: "{{count}} 个任务运行中", en: "{{count}} tasks running", fr: "{{count}} tâches en cours", ja: "{{count}} 件のタスクを実行中", ko: "{{count}}개 작업 실행 중" },
  workspaceUnset: { zh: "未设置工作目录", en: "Workspace not set", fr: "Espace de travail non défini", ja: "ワークスペース未設定", ko: "작업 공간이 설정되지 않음" },
  providerRoutingDescription: { zh: "按优先级自动切换，API Key 仅显示保存状态。", en: "Automatically fail over by priority. API keys only show their saved state.", fr: "Basculement automatique par priorité. Seul l’état enregistré des clés API est affiché.", ja: "優先度順に自動切り替えします。API Key は保存状態のみ表示します。", ko: "우선순위에 따라 자동 전환합니다. API Key는 저장 상태만 표시합니다." },
  sync: { zh: "同步", en: "Sync", fr: "Synchroniser", ja: "同期", ko: "동기화" },
  checkAll: { zh: "全部检查", en: "Check all", fr: "Tout vérifier", ja: "すべて確認", ko: "모두 확인" },
  add: { zh: "新增", en: "Add", fr: "Ajouter", ja: "追加", ko: "추가" },
  noProviders: { zh: "还没有 Provider，点击“新增”配置第一个路由", en: "No providers yet. Select Add to configure the first route.", fr: "Aucun provider. Sélectionnez Ajouter pour configurer la première route.", ja: "Provider がありません。「追加」から最初のルートを設定してください。", ko: "Provider가 없습니다. 추가를 눌러 첫 경로를 설정하세요." },
  current: { zh: "当前", en: "Current", fr: "Actuel", ja: "現在", ko: "현재" },
  unavailable: { zh: "不可用", en: "Unavailable", fr: "Indisponible", ja: "利用不可", ko: "사용 불가" },
  use: { zh: "使用", en: "Use", fr: "Utiliser", ja: "使用", ko: "사용" },
  default: { zh: "默认", en: "Default", fr: "Par défaut", ja: "既定", ko: "기본" },
  priority: { zh: "优先级 {{value}}", en: "Priority {{value}}", fr: "Priorité {{value}}", ja: "優先度 {{value}}", ko: "우선순위 {{value}}" },
  excludedFromFailover: { zh: "不参与切换", en: "Excluded from failover", fr: "Exclu du basculement", ja: "切り替え対象外", ko: "전환에서 제외" },
  apiKeyUnset: { zh: "未设置 API Key", en: "API key not set", fr: "Clé API non définie", ja: "API Key 未設定", ko: "API Key 미설정" },
  latency: { zh: "延迟 {{value}}", en: "Latency {{value}}", fr: "Latence {{value}}", ja: "遅延 {{value}}", ko: "지연 {{value}}" },
  successFailure: { zh: "成功 {{success}} / 失败 {{failure}}", en: "{{success}} succeeded / {{failure}} failed", fr: "{{success}} réussites / {{failure}} échecs", ja: "成功 {{success}} / 失敗 {{failure}}", ko: "성공 {{success}} / 실패 {{failure}}" },
  check: { zh: "检查", en: "Check", fr: "Vérifier", ja: "確認", ko: "확인" },
  codexDefaultAddress: { zh: "Codex 默认地址", en: "Codex default URL", fr: "URL Codex par défaut", ja: "Codex 既定 URL", ko: "Codex 기본 URL" },
  recycleBinDescription: { zh: "删除会话会先移动到这里；只有在此处永久删除才会销毁 Codex 线程。", en: "Deleted sessions move here first. Only permanent deletion destroys a Codex thread.", fr: "Les sessions supprimées arrivent d’abord ici. Seule la suppression définitive détruit un fil Codex.", ja: "削除したセッションはまずここへ移動します。完全削除した場合のみ Codex スレッドが破棄されます。", ko: "삭제된 세션은 먼저 여기로 이동합니다. 영구 삭제할 때만 Codex 스레드가 제거됩니다." },
  inactiveTrashReason: { zh: "30 天无交互，已自动移入", en: "Automatically moved after 30 days without interaction", fr: "Déplacée automatiquement après 30 jours sans interaction", ja: "30 日間操作がないため自動移動", ko: "30일 동안 상호작용이 없어 자동 이동됨" },
  readingTrash: { zh: "正在读取回收站…", en: "Loading trash…", fr: "Chargement de la corbeille…", ja: "ゴミ箱を読み込み中…", ko: "휴지통 불러오는 중…" },
  deleted: { zh: "已删除", en: "Deleted", fr: "Supprimé", ja: "削除済み", ko: "삭제됨" },
  restore: { zh: "恢复", en: "Restore", fr: "Restaurer", ja: "復元", ko: "복원" },
  permanentDelete: { zh: "永久删除", en: "Delete permanently", fr: "Supprimer définitivement", ja: "完全に削除", ko: "영구 삭제" },
  emptyTrash: { zh: "回收站为空", en: "Trash is empty", fr: "La corbeille est vide", ja: "ゴミ箱は空です", ko: "휴지통이 비어 있습니다" },
  serverConnection: { zh: "服务器连接", en: "Server connection", fr: "Connexion serveur", ja: "サーバー接続", ko: "서버 연결" },
  sshConnectionDescription: { zh: "打开网址后自动尝试免密 SSH；只有认证失败时才填写一次 SSH 用户名和密码。", en: "Passwordless SSH is attempted automatically. Credentials are requested only if authentication fails.", fr: "SSH sans mot de passe est tenté automatiquement. Les identifiants ne sont demandés qu’en cas d’échec.", ja: "パスワードなし SSH を自動で試行し、認証に失敗した場合のみ資格情報を求めます。", ko: "비밀번호 없는 SSH를 자동으로 시도하며 인증 실패 시에만 자격 증명을 요청합니다." },
  healthHealthy: { zh: "正常", en: "Healthy", fr: "Opérationnel", ja: "正常", ko: "정상" },
  healthReachable: { zh: "可达", en: "Reachable", fr: "Accessible", ja: "到達可能", ko: "연결 가능" },
  healthConfigured: { zh: "已配置", en: "Configured", fr: "Configuré", ja: "設定済み", ko: "설정됨" },
  healthNeedsKey: { zh: "缺少密钥", en: "Key required", fr: "Clé requise", ja: "キーが必要", ko: "키 필요" },
  healthAuthError: { zh: "鉴权失败", en: "Authentication failed", fr: "Échec d’authentification", ja: "認証失敗", ko: "인증 실패" },
  healthDegraded: { zh: "服务异常", en: "Degraded", fr: "Dégradé", ja: "サービス異常", ko: "서비스 저하" },
  healthUnreachable: { zh: "不可达", en: "Unreachable", fr: "Inaccessible", ja: "到達不可", ko: "연결 불가" },
  healthInvalid: { zh: "配置错误", en: "Invalid configuration", fr: "Configuration incorrecte", ja: "設定エラー", ko: "설정 오류" },
  healthError: { zh: "运行失败", en: "Runtime failure", fr: "Échec d’exécution", ja: "実行失敗", ko: "실행 실패" },
  healthUnchecked: { zh: "未检查", en: "Not checked", fr: "Non vérifié", ja: "未確認", ko: "확인되지 않음" },
  readyStatus: { zh: "已就绪", en: "Ready", fr: "Prêt", ja: "準備完了", ko: "준비됨" },
  useLocal: { zh: "使用本机", en: "Use local", fr: "Utiliser en local", ja: "ローカルを使用", ko: "로컬 사용" },
  noFixedServer: { zh: "还没有固定服务器，请输入地址连接", en: "No fixed server yet. Enter an address to connect.", fr: "Aucun serveur fixe. Saisissez une adresse pour vous connecter.", ja: "固定サーバーがありません。アドレスを入力して接続してください。", ko: "고정 서버가 없습니다. 연결할 주소를 입력하세요." },
  serverAddress: { zh: "服务器地址", en: "Server address", fr: "Adresse du serveur", ja: "サーバーアドレス", ko: "서버 주소" },
  sshUsername: { zh: "SSH 用户名", en: "SSH username", fr: "Nom d’utilisateur SSH", ja: "SSH ユーザー名", ko: "SSH 사용자 이름" },
  port: { zh: "端口", en: "Port", fr: "Port", ja: "ポート", ko: "포트" },
  passwordOrPassphrase: { zh: "密码或私钥口令", en: "Password or key passphrase", fr: "Mot de passe ou phrase secrète", ja: "パスワードまたは秘密鍵パスフレーズ", ko: "비밀번호 또는 개인 키 암호" },
  connectServer: { zh: "连接服务器", en: "Connect server", fr: "Connecter le serveur", ja: "サーバーに接続", ko: "서버 연결" },
  defaultUser: { zh: "默认用户", en: "Default user", fr: "Utilisateur par défaut", ja: "既定ユーザー", ko: "기본 사용자" },
  installCodex: { zh: "安装 Codex", en: "Install Codex", fr: "Installer Codex", ja: "Codex をインストール", ko: "Codex 설치" },
  disconnect: { zh: "断开", en: "Disconnect", fr: "Déconnecter", ja: "切断", ko: "연결 해제" },
  login: { zh: "登录", en: "Log in", fr: "Se connecter", ja: "ログイン", ko: "로그인" },
  retry: { zh: "重试", en: "Retry", fr: "Réessayer", ja: "再試行", ko: "다시 시도" },
  connected: { zh: "已连接", en: "Connected", fr: "Connecté", ja: "接続済み", ko: "연결됨" },
  codexUnavailable: { zh: "缺少 Codex", en: "Codex unavailable", fr: "Codex indisponible", ja: "Codex なし", ko: "Codex 없음" },
  needsPassword: { zh: "需要密码", en: "Password required", fr: "Mot de passe requis", ja: "パスワードが必要", ko: "비밀번호 필요" },
  connectionFailed: { zh: "连接失败", en: "Connection failed", fr: "Échec de connexion", ja: "接続失敗", ko: "연결 실패" },
  notConnected: { zh: "未连接", en: "Not connected", fr: "Non connecté", ja: "未接続", ko: "연결되지 않음" },
  summary: { zh: "摘要", en: "Summary", fr: "Résumé", ja: "要約", ko: "요약" },
  rawMemory: { zh: "原始记忆", en: "Raw memory", fr: "Mémoire brute", ja: "元のメモリ", ko: "원본 메모리" },
  undo: { zh: "撤销", en: "Undo", fr: "Annuler", ja: "元に戻す", ko: "실행 취소" },
  redo: { zh: "重做", en: "Redo", fr: "Rétablir", ja: "やり直す", ko: "다시 실행" },
  bold: { zh: "粗体", en: "Bold", fr: "Gras", ja: "太字", ko: "굵게" },
  italic: { zh: "斜体", en: "Italic", fr: "Italique", ja: "斜体", ko: "기울임꼴" },
  heading: { zh: "标题", en: "Heading", fr: "Titre", ja: "見出し", ko: "제목" },
  link: { zh: "链接", en: "Link", fr: "Lien", ja: "リンク", ko: "링크" },
  inlineCode: { zh: "行内代码", en: "Inline code", fr: "Code en ligne", ja: "インラインコード", ko: "인라인 코드" },
  bulletList: { zh: "项目列表", en: "Bullet list", fr: "Liste à puces", ja: "箇条書き", ko: "글머리 기호 목록" },
  wrapLines: { zh: "自动换行", en: "Wrap lines", fr: "Retour à la ligne", ja: "行を折り返す", ko: "자동 줄 바꿈" },
  editView: { zh: "编辑", en: "Edit", fr: "Modifier", ja: "編集", ko: "편집" },
  splitView: { zh: "分栏", en: "Split", fr: "Partagé", ja: "分割", ko: "분할" },
  preview: { zh: "预览", en: "Preview", fr: "Aperçu", ja: "プレビュー", ko: "미리 보기" },
  lineColumn: { zh: "第 {{line}} 行，第 {{column}} 列", en: "Ln {{line}}, Col {{column}}", fr: "Lig {{line}}, Col {{column}}", ja: "{{line}} 行 {{column}} 列", ko: "{{line}}행 {{column}}열" },
  memoryEditorPlaceholder: { zh: "用 Markdown 记录项目约定、重要结论或长期上下文…", en: "Record project conventions, important conclusions, or long-term context in Markdown…", fr: "Notez les conventions, conclusions importantes ou le contexte durable en Markdown…", ja: "プロジェクトの規約、重要な結論、長期的な文脈を Markdown で記録…", ko: "프로젝트 규칙, 중요한 결론 또는 장기 컨텍스트를 Markdown으로 기록하세요…" },
  skillTemplate: { zh: "# 使用说明\n\n说明 Codex 应在什么情况下使用这个 Skill，以及必须遵循的步骤。\n", en: "# Instructions\n\nDescribe when Codex should use this skill and the steps it must follow.\n", fr: "# Instructions\n\nDécrivez quand Codex doit utiliser ce skill et les étapes à suivre.\n", ja: "# 使用方法\n\nCodex がこの Skill を使用する条件と従うべき手順を記述します。\n", ko: "# 사용 방법\n\nCodex가 이 Skill을 사용해야 하는 상황과 따라야 할 단계를 설명하세요.\n" },
  codexPartner: { zh: "Codex Partner", en: "Codex Partner", fr: "Codex Partner", ja: "Codex Partner", ko: "Codex Partner" },
  messageFromTerminal: { zh: "来自终端", en: "From terminal", fr: "Depuis le terminal", ja: "ターミナルから", ko: "터미널에서" },
  phaseGenerating: { zh: "正在组织回复", en: "Composing the response", fr: "Rédaction de la réponse", ja: "回答を作成中", ko: "답변 작성 중" },
  phasePlanning: { zh: "正在分析并规划下一步", en: "Analyzing and planning the next step", fr: "Analyse et planification de la suite", ja: "分析して次の手順を計画中", ko: "분석하고 다음 단계 계획 중" },
  phaseCommand: { zh: "正在运行命令并读取结果", en: "Running a command and reading the result", fr: "Exécution d’une commande et lecture du résultat", ja: "コマンドを実行して結果を確認中", ko: "명령을 실행하고 결과 확인 중" },
  phaseFiles: { zh: "正在整理工作区文件", en: "Working through workspace files", fr: "Traitement des fichiers de l’espace", ja: "ワークスペースのファイルを処理中", ko: "작업 공간 파일 처리 중" },
  phaseTools: { zh: "正在调用工具完成这一步", en: "Using a tool for this step", fr: "Utilisation d’un outil pour cette étape", ja: "この手順でツールを使用中", ko: "이 단계에 도구 사용 중" },
  phaseSearch: { zh: "正在检索需要的资料", en: "Looking up the needed information", fr: "Recherche des informations nécessaires", ja: "必要な情報を検索中", ko: "필요한 정보 검색 중" },
  phaseCompact: { zh: "正在压缩上下文，保留关键线索", en: "Compacting context while keeping key details", fr: "Compression du contexte en conservant l’essentiel", ja: "重要な情報を残してコンテキストを圧縮中", ko: "핵심 정보를 유지하며 컨텍스트 압축 중" },
  phaseProvider: { zh: "正在切换可用的 Provider", en: "Switching to an available provider", fr: "Basculement vers un provider disponible", ja: "利用可能な Provider に切り替え中", ko: "사용 가능한 Provider로 전환 중" },
  phaseTerminalSynced: { zh: "已接入同一任务的终端进度", en: "Following the same task from the terminal", fr: "Suivi de la même tâche depuis le terminal", ja: "同じタスクのターミナル進捗に接続済み", ko: "같은 작업의 터미널 진행 상황 연결됨" },
  phaseInstruction: { zh: "已收到新的指令", en: "New instruction received", fr: "Nouvelle instruction reçue", ja: "新しい指示を受信", ko: "새 지시 수신됨" },
  phaseInteraction: { zh: "正在处理 Codex 交互请求", en: "Handling a Codex interaction request", fr: "Traitement d’une requête interactive Codex", ja: "Codex の対話リクエストを処理中", ko: "Codex 상호작용 요청 처리 중" },
  phaseQueued: { zh: "已排队，等待服务端接管", en: "Queued and waiting for the service", fr: "En file d’attente du service", ja: "キューでサービスの開始待ち", ko: "대기열에서 서비스 시작 대기 중" },
  phaseAnalyzing: { zh: "正在读懂任务和上下文", en: "Reading the task and its context", fr: "Lecture de la tâche et de son contexte", ja: "タスクとコンテキストを確認中", ko: "작업과 컨텍스트 파악 중" },
  phaseRetry: { zh: "正在重新连接并继续当前 Goal", en: "Reconnecting and continuing the current goal", fr: "Reconnexion et poursuite de l’objectif actuel", ja: "再接続して現在の Goal を継続中", ko: "다시 연결하여 현재 Goal 계속 진행 중" },
  workingTitle: { zh: "Codex 正在工作", en: "Codex is working", fr: "Codex travaille", ja: "Codex が作業中", ko: "Codex 작업 중" },
  recoveringTitle: { zh: "Codex 正在恢复", en: "Codex is recovering", fr: "Codex reprend", ja: "Codex が復旧中", ko: "Codex 복구 중" },
  queuedTitle: { zh: "任务等待运行", en: "Task waiting to run", fr: "Tâche en attente", ja: "タスクは実行待ち", ko: "작업 실행 대기 중" },
  standbyTip: { zh: "准备好了，下一条消息会从这里开始", en: "Ready when you are; the next message starts here", fr: "Prêt : le prochain message démarre ici", ja: "準備完了。次のメッセージから開始します", ko: "준비 완료. 다음 메시지부터 시작합니다" },
  wastelandStandbyTip: { zh: "侦察机已待命 · 等待新指令", en: "Scout online · awaiting a new directive", fr: "Éclaireur en ligne · en attente d’instructions", ja: "偵察ユニット待機中 · 新しい指示を待っています", ko: "정찰 유닛 대기 중 · 새 지시를 기다리는 중" },
  terminalWorkingTitle: { zh: "Codex 正在工作 · 终端已同步", en: "Codex is working · terminal synced", fr: "Codex travaille · terminal synchronisé", ja: "Codex が作業中 · ターミナル同期済み", ko: "Codex 작업 중 · 터미널 동기화됨" },
  activeTurns: { zh: "{{count}} 个 turn", en: "{{count}} turns", fr: "{{count}} tours", ja: "{{count}} ターン", ko: "{{count}}개 turn" },
  tipSteps: { zh: "正在把线索拼成答案", en: "Turning clues into an answer", fr: "Les indices deviennent une réponse", ja: "手がかりを答えにまとめています", ko: "단서를 답으로 엮는 중" },
  tipContext: { zh: "正在检查有没有漏掉一步", en: "Checking for a missed step", fr: "Recherche d’une étape oubliée", ja: "見落とした手順がないか確認中", ko: "놓친 단계가 없는지 확인 중" },
  tipEvidence: { zh: "正在给答案做最后抛光", en: "Giving the answer a final polish", fr: "Dernière touche à la réponse", ja: "答えを最後に磨いています", ko: "답변을 마지막으로 다듬는 중" },
  tipSteer: { zh: "需要时可把新消息插入当前回合", en: "New messages can be steered into the current turn", fr: "Les nouveaux messages peuvent être insérés dans le tour actuel", ja: "新しいメッセージを現在のターンに挿入できます", ko: "새 메시지를 현재 turn에 삽입할 수 있습니다" },
  tipGoal: { zh: "Goal 正沿着同一条线继续", en: "The goal keeps moving on this thread", fr: "L’objectif avance dans ce fil", ja: "Goal はこのスレッドで続いています", ko: "Goal은 이 스레드에서 계속됩니다" },
  tipContinue: { zh: "可以先喝口水，这里还在推进", en: "Take a short break; work continues here", fr: "Petite pause possible, le travail continue ici", ja: "ひと息ついても、作業は続いています", ko: "잠시 쉬어도 여기서 계속 진행됩니다" },
  queue: { zh: "队列", en: "Queue", fr: "File", ja: "キュー", ko: "대기열" },
  queueEmpty: { zh: "队列为空，连续消息会在这里等待", en: "Queue is empty; messages sent in sequence wait here", fr: "File vide : les messages successifs attendront ici", ja: "キューは空です。続けて送るメッセージはここで待機します", ko: "대기열이 비어 있습니다. 연속 메시지는 여기서 기다립니다" },
  approvalCommand: { zh: "Codex 请求执行命令", en: "Codex requests command execution", fr: "Codex demande d’exécuter une commande", ja: "Codex がコマンド実行を要求", ko: "Codex가 명령 실행을 요청합니다" },
  approvalFile: { zh: "Codex 请求修改文件", en: "Codex requests file changes", fr: "Codex demande de modifier des fichiers", ja: "Codex がファイル変更を要求", ko: "Codex가 파일 변경을 요청합니다" },
  approvalPermissions: { zh: "Codex 请求额外权限", en: "Codex requests additional access", fr: "Codex demande des autorisations", ja: "Codex が追加権限を要求", ko: "Codex가 추가 권한을 요청합니다" },
  approvalQuestion: { zh: "Codex 需要你的选择", en: "Codex needs your input", fr: "Codex attend votre choix", ja: "Codex が入力を待っています", ko: "Codex가 선택을 기다립니다" },
  approvalWaiting: { zh: "受控模式 · 等待授权", en: "Controlled mode · awaiting approval", fr: "Mode contrôlé · autorisation requise", ja: "制御モード · 承認待ち", ko: "제어 모드 · 승인 대기" },
  approvalReason: { zh: "原因", en: "Reason", fr: "Motif", ja: "理由", ko: "이유" },
  approvalWorkspace: { zh: "目录", en: "Directory", fr: "Dossier", ja: "ディレクトリ", ko: "디렉터리" },
  approvalDeny: { zh: "拒绝", en: "Deny", fr: "Refuser", ja: "拒否", ko: "거부" },
  approvalOnce: { zh: "允许一次", en: "Allow once", fr: "Autoriser une fois", ja: "今回のみ許可", ko: "한 번 허용" },
  approvalSession: { zh: "本会话允许", en: "Allow for session", fr: "Autoriser pour la session", ja: "セッション中許可", ko: "세션 동안 허용" },
  approvalSubmit: { zh: "提交选择", en: "Submit", fr: "Envoyer", ja: "送信", ko: "제출" },
  approvalResolved: { zh: "授权选择已执行", en: "Approval choice applied", fr: "Choix appliqué", ja: "承認を反映しました", ko: "승인 선택을 적용했습니다" },
  queuedAfterTurn: { zh: "已加入队列，当前 turn 结束后自动发射", en: "Queued and will run after the current turn", fr: "Ajouté à la file et exécuté après le tour actuel", ja: "キューに追加し、現在のターン完了後に自動実行します", ko: "대기열에 추가되었으며 현재 turn 종료 후 자동 실행됩니다" },
  clearQueue: { zh: "清空所有排队消息", en: "Clear all queued messages", fr: "Vider tous les messages en attente", ja: "待機メッセージをすべて消去", ko: "대기 메시지 모두 지우기" },
  dispatchNow: { zh: "立即执行这条消息", en: "Run this message now", fr: "Exécuter ce message maintenant", ja: "このメッセージを今すぐ実行", ko: "이 메시지 지금 실행" },
  editQueuedAction: { zh: "编辑排队消息", en: "Edit queued message", fr: "Modifier le message en attente", ja: "待機メッセージを編集", ko: "대기 메시지 편집" },
  deleteQueuedAction: { zh: "删除排队消息", en: "Delete queued message", fr: "Supprimer le message en attente", ja: "待機メッセージを削除", ko: "대기 메시지 삭제" },
  queueMore: { zh: "还有 {{count}} 条", en: "{{count}} more", fr: "{{count}} de plus", ja: "あと {{count}} 件", ko: "{{count}}개 더 있음" },
  fileChanged: { zh: "文件已更新", en: "File updated", fr: "Fichier mis à jour", ja: "ファイルを更新", ko: "파일 업데이트됨" },
  contextCompressed: { zh: "上下文已压缩", en: "Context compacted", fr: "Contexte compacté", ja: "コンテキストを圧縮しました", ko: "컨텍스트 압축됨" },
  contextRemaining: { zh: "剩余", en: "left", fr: "restant", ja: "残り", ko: "남음" },
  contextUsage: { zh: "上下文容量", en: "Context capacity", fr: "Capacité du contexte", ja: "コンテキスト容量", ko: "컨텍스트 용량" },
  terminalTurnStarted: { zh: "同一任务的终端 turn 已开始", en: "A terminal turn started on this task", fr: "Un tour terminal a démarré sur cette tâche", ja: "同じタスクでターミナル turn が開始しました", ko: "같은 작업에서 터미널 turn 시작됨" },
  terminalTurnCompleted: { zh: "终端 turn 已完成", en: "Terminal turn completed", fr: "Tour terminal terminé", ja: "ターミナル turn が完了しました", ko: "터미널 turn 완료됨" },
  terminalTurnAborted: { zh: "终端 turn 已中断", en: "Terminal turn interrupted", fr: "Tour terminal interrompu", ja: "ターミナル turn が中断しました", ko: "터미널 turn 중단됨" },
  terminalRemoteShell: { zh: "远程 Shell", en: "Remote shell", fr: "Shell distant", ja: "リモートシェル", ko: "원격 셸" },
  terminalTitle: { zh: "远程终端", en: "Remote terminal", fr: "Terminal distant", ja: "リモートターミナル", ko: "원격 터미널" },
  terminalNotConnected: { zh: "未连接", en: "Not connected", fr: "Non connecté", ja: "未接続", ko: "연결되지 않음" },
  terminalConnecting: { zh: "连接中", en: "Connecting", fr: "Connexion", ja: "接続中", ko: "연결 중" },
  terminalReconnecting: { zh: "重连中", en: "Reconnecting", fr: "Reconnexion", ja: "再接続中", ko: "재연결 중" },
  terminalConnected: { zh: "已连接", en: "Connected", fr: "Connecté", ja: "接続済み", ko: "연결됨" },
  terminalModuleError: { zh: "终端模块加载失败", en: "Terminal module failed to load", fr: "Échec du chargement du terminal", ja: "ターミナルモジュールの読み込みに失敗", ko: "터미널 모듈 로드 실패" },
  terminalModuleToast: { zh: "终端组件未加载，请刷新页面重试", en: "Terminal components are unavailable; refresh and try again", fr: "Composants indisponibles ; actualisez puis réessayez", ja: "ターミナル部品を読み込めません。更新して再試行してください", ko: "터미널 구성 요소를 사용할 수 없습니다. 새로 고친 후 다시 시도하세요" },
  terminalOutput: { zh: "远程终端输出", en: "Remote terminal output", fr: "Sortie du terminal distant", ja: "リモートターミナル出力", ko: "원격 터미널 출력" },
  terminalHint: { zh: "点击终端后输入 · Enter 执行 · Ctrl+C 中断", en: "Click the terminal to type · Enter runs · Ctrl+C interrupts", fr: "Cliquez dans le terminal · Entrée exécute · Ctrl+C interrompt", ja: "ターミナルをクリックして入力 · Enterで実行 · Ctrl+Cで中断", ko: "터미널을 클릭해 입력 · Enter 실행 · Ctrl+C 중단" },
  terminalReconnect: { zh: "重连", en: "Reconnect", fr: "Reconnecter", ja: "再接続", ko: "재연결" },
  terminalClose: { zh: "关闭终端", en: "Close terminal", fr: "Fermer le terminal", ja: "ターミナルを閉じる", ko: "터미널 닫기" },
  selectSessionFirst: { zh: "请先选择一个会话", en: "Select a session first", fr: "Sélectionnez d’abord une session", ja: "先にセッションを選択してください", ko: "먼저 세션을 선택하세요" },
  terminalReconnectNotice: { zh: "连接中断，正在重连", en: "Connection interrupted; reconnecting", fr: "Connexion interrompue ; reconnexion", ja: "接続が中断されました。再接続中", ko: "연결이 끊겨 재연결 중" },
  providerSwitched: { zh: "Provider 已切换：{{from}} → {{to}}", en: "Provider switched: {{from}} → {{to}}", fr: "Provider changé : {{from}} → {{to}}", ja: "Provider を切り替えました：{{from}} → {{to}}", ko: "Provider 전환됨: {{from}} → {{to}}" },
  nextProvider: { zh: "下一个", en: "next", fr: "suivant", ja: "次", ko: "다음" },
  codexActivity: { zh: "Codex 活动", en: "Codex activity", fr: "Activité Codex", ja: "Codex アクティビティ", ko: "Codex 활동" },
  confirmTitle: { zh: "确认操作", en: "Confirm action", fr: "Confirmer l’action", ja: "操作の確認", ko: "작업 확인" },
  confirmAction: { zh: "确认", en: "Confirm", fr: "Confirmer", ja: "確認", ko: "확인" },
  authTitle: { zh: "连接 Codex Partner", en: "Connect to Codex Partner", fr: "Connexion à Codex Partner", ja: "Codex Partner に接続", ko: "Codex Partner 연결" },
  authMessage: { zh: "使用这台服务器的 SSH 账户登录。密码只用于本次 SSH 验证，不会保存。", en: "Sign in with an SSH account on this server. The password is used only for this verification and is not stored.", fr: "Connectez-vous avec un compte SSH de ce serveur. Le mot de passe sert uniquement à cette vérification et n’est pas stocké.", ja: "このサーバーの SSH アカウントでログインします。パスワードは今回の認証にのみ使用され、保存されません。", ko: "이 서버의 SSH 계정으로 로그인하세요. 비밀번호는 이번 인증에만 사용되며 저장되지 않습니다." },
  sshUsername: { zh: "SSH 用户名", en: "SSH username", fr: "Nom d’utilisateur SSH", ja: "SSH ユーザー名", ko: "SSH 사용자 이름" },
  sshPassword: { zh: "SSH 密码", en: "SSH password", fr: "Mot de passe SSH", ja: "SSH パスワード", ko: "SSH 비밀번호" },
  sshLoginFailed: { zh: "SSH 登录失败", en: "SSH sign-in failed", fr: "Échec de la connexion SSH", ja: "SSH ログインに失敗しました", ko: "SSH 로그인 실패" },
  connect: { zh: "连接", en: "Connect", fr: "Se connecter", ja: "接続", ko: "연결" },
  renameSessionTitle: { zh: "重命名会话", en: "Rename session", fr: "Renommer la session", ja: "セッション名を変更", ko: "세션 이름 변경" },
  sessionName: { zh: "会话名称", en: "Session name", fr: "Nom de la session", ja: "セッション名", ko: "세션 이름" },
  permanentDeleteConfirm: { zh: "永久删除后无法恢复，Codex 线程记录也会被销毁。", en: "Permanent deletion cannot be undone and destroys the Codex thread record.", fr: "La suppression définitive est irréversible et détruit le fil Codex.", ja: "完全削除は取り消せず、Codex スレッドの記録も破棄されます。", ko: "영구 삭제는 취소할 수 없으며 Codex 스레드 기록도 제거됩니다." },
  clearQueueConfirm: { zh: "清空所有排队消息？当前正在运行的 Codex turn 不会受到影响。", en: "Clear all queued messages? The current Codex turn will keep running.", fr: "Vider tous les messages en attente ? Le tour Codex actuel continuera.", ja: "待機メッセージをすべて消去しますか？現在の Codex turn は継続します。", ko: "대기 메시지를 모두 지울까요? 현재 Codex turn은 계속 실행됩니다." },
  deleteMemoryConfirm: { zh: "删除这个 Codex 记忆文件？此操作无法撤销。", en: "Delete this Codex memory file? This cannot be undone.", fr: "Supprimer ce fichier mémoire Codex ? Cette action est irréversible.", ja: "この Codex メモリファイルを削除しますか？元に戻せません。", ko: "이 Codex 메모리 파일을 삭제할까요? 취소할 수 없습니다." },
  rebuildMemoryConfirm: { zh: "重建线程生成记忆？现有生成内容将被重新计算。", en: "Rebuild generated thread memory? Existing generated content will be recalculated.", fr: "Reconstruire la mémoire générée ? Le contenu existant sera recalculé.", ja: "生成スレッドメモリを再構築しますか？既存の内容は再計算されます。", ko: "생성된 스레드 메모리를 다시 만들까요? 기존 내용이 다시 계산됩니다." },
  deleteProviderConfirm: { zh: "删除这个 Provider？使用它的会话将回到自动选择。", en: "Delete this provider? Sessions using it will return to automatic selection.", fr: "Supprimer ce provider ? Les sessions associées repasseront en sélection automatique.", ja: "この Provider を削除しますか？使用中のセッションは自動選択に戻ります。", ko: "이 Provider를 삭제할까요? 사용 중인 세션은 자동 선택으로 돌아갑니다." },
  deleteSkillConfirm: { zh: "删除这个 Skill？对应的 Skill 文件将被移除。", en: "Delete this skill? Its skill file will be removed.", fr: "Supprimer ce skill ? Son fichier sera supprimé.", ja: "この Skill を削除しますか？対応するファイルも削除されます。", ko: "이 Skill을 삭제할까요? 해당 Skill 파일도 제거됩니다." },
  protectedSkillDelete: { zh: "系统 Skill 受保护，不能删除", en: "System skills are protected and cannot be deleted", fr: "Les skills système sont protégés et ne peuvent pas être supprimés", ja: "システム Skill は保護されているため削除できません", ko: "시스템 Skill은 보호되어 삭제할 수 없습니다" },
  archiveSessionConfirm: { zh: "归档这个会话？之后仍可从归档中恢复。", en: "Archive this session? It can be restored later.", fr: "Archiver cette session ? Elle pourra être restaurée plus tard.", ja: "このセッションをアーカイブしますか？後で復元できます。", ko: "이 세션을 보관할까요? 나중에 복원할 수 있습니다." },
  trashSessionConfirm: { zh: "把这个会话移到回收站？在永久删除前仍可恢复。", en: "Move this session to trash? It can be restored before permanent deletion.", fr: "Mettre cette session à la corbeille ? Elle reste récupérable avant suppression définitive.", ja: "このセッションをゴミ箱へ移動しますか？完全削除前なら復元できます。", ko: "이 세션을 휴지통으로 옮길까요? 영구 삭제 전에는 복원할 수 있습니다." },
  clearGoalConfirm: { zh: "清空当前 Goal？Goal 自动续跑也会同时关闭。", en: "Clear the current goal? Goal auto-resume will also turn off.", fr: "Effacer l’objectif actuel ? La reprise automatique sera aussi désactivée.", ja: "現在の Goal を消去しますか？自動再開も無効になります。", ko: "현재 Goal을 지울까요? Goal 자동 재개도 함께 꺼집니다." },
  overwriteFileConfirm: { zh: "{{name}} 已存在。要用新文件覆盖它吗？", en: "{{name}} already exists. Replace it with the new file?", fr: "{{name}} existe déjà. Le remplacer par le nouveau fichier ?", ja: "{{name}} は既に存在します。新しいファイルで上書きしますか？", ko: "{{name}} 파일이 이미 있습니다. 새 파일로 덮어쓸까요?" },
  destructiveCommandConfirm: { zh: "{{command}} 会修改或删除持久内容。确认继续执行？", en: "{{command}} changes or deletes persistent content. Continue?", fr: "{{command}} modifie ou supprime du contenu persistant. Continuer ?", ja: "{{command}} は永続データを変更または削除します。続行しますか？", ko: "{{command}} 명령은 영구 콘텐츠를 변경하거나 삭제합니다. 계속할까요?" },
};
const I18N_RESOURCES = Object.fromEntries(Object.keys(LANGUAGE_PACKS).map(language => {
  const translation = { ...LANGUAGE_PACKS[language], ...(LANGUAGE_EXTRAS[language] || {}) };
  for (const [key, values] of Object.entries(UI_LABELS)) translation[key] = values[language] || values.zh || key;
  return [language, { translation }];
}));
if (!window.i18next) throw new Error("i18next failed to load");
window.i18next.init({
  lng: state.language,
  fallbackLng: "zh",
  supportedLngs: Object.keys(LANGUAGE_PACKS),
  resources: I18N_RESOURCES,
  initImmediate: false,
  interpolation: { escapeValue: false },
});
function uiLabel(key, options = {}) { return t(key, key, options); }
function mascotMarkup(className = "") { return `<span class="mascot mascot-inline ${className}" aria-hidden="true"><i></i><b></b><em></em></span>`; }
function humanMarkup(className = "") {
  if (state.userAvatar) return `<span class="human-avatar image-avatar ${className}" aria-hidden="true"><img class="human-avatar-image" src="${esc(state.userAvatar)}" alt="" /></span>`;
  return `<span class="human-avatar ${className}" aria-hidden="true"><i></i><b></b></span>`;
}
function applyUserProfile(profile, rerender = false) {
  const next = profile?.avatar_url || DEFAULT_USER_AVATAR;
  const changed = next !== state.userAvatar;
  state.userAvatar = next;
  if (profile?.avatar_url) localStorage.removeItem("codex-dashboard-user-avatar");
  if (changed && rerender && typeof renderChat === "function") renderChat();
}
async function loadUserProfile() {
  let profile = await api("/profile");
  if (!profile.avatar_url && legacyUserAvatar.startsWith("data:image/")) {
    try {
      profile = await api("/profile/avatar", { method: "PUT", body: JSON.stringify({ data_url: legacyUserAvatar }) });
    } catch (_) {
      return profile;
    }
  }
  applyUserProfile(profile);
  return profile;
}
function updateThemeToggle() {
  const toggle = document.querySelector("#theme-toggle");
  if (!toggle) return;
  const wasteland = document.documentElement.dataset.theme === "wasteland";
  const label = uiLabel(wasteland ? "darkTheme" : "wastelandTheme");
  toggle.title = label; toggle.setAttribute("aria-label", label); toggle.setAttribute("aria-pressed", wasteland ? "true" : "false");
}
function standbyTipLabel() { return uiLabel(document.documentElement.dataset.theme === "wasteland" ? "wastelandStandbyTip" : "standbyTip"); }
function setTheme(theme, announce = false) {
  const next = theme === "wasteland" ? "wasteland" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("codex-dashboard-theme", next);
  updateThemeToggle();
  const progress = document.querySelector("#turn-progress");
  if (progress?.classList.contains("idle")) document.querySelector("#turn-progress-tip").textContent = standbyTipLabel();
  if (announce) toast(uiLabel(next === "wasteland" ? "wastelandEnabled" : "darkEnabled"));
}
function toggleTheme() { setTheme(document.documentElement.dataset.theme === "wasteland" ? "dark" : "wasteland", true); }
function applyLanguage(language = state.language) {
  state.language = LANGUAGE_PACKS[language] ? language : "zh";
  window.i18next.changeLanguage(state.language);
  localStorage.setItem("codex-dashboard-language", state.language);
  document.documentElement.lang = state.language === "zh" ? "zh-CN" : state.language;
  const text = { "#new-task span": "newSession", ".shortcut-hint:nth-child(1) .shortcut-label": "terminal", ".shortcut-hint:nth-child(2) .shortcut-label": "shortcutNext", ".shortcut-hint:nth-child(3) .shortcut-label": "shortcutPrevious", ".shortcut-hint:nth-child(4) .shortcut-label": "shortcutFirst", ".shortcut-hint:nth-child(5) .shortcut-label": "shortcutComposer", ".sidebar-top h1": "sessions", ".filter[data-filter=all]": "all", ".filter[data-filter=running]": "running", ".filter[data-filter=available]": "available", ".trash-filter": "trash", ".sidebar-link[data-panel=memories] span": "memories", ".sidebar-link[data-panel=skills] span": "skills", "#empty-conversation h2": "emptyTitle", "#empty-conversation p": "emptyCopy", "#empty-new-task": "create", "#send-codex-label": "send", "#message-input": "composerPlaceholder", "#session-search": "searchSessions", ".composer-model-control > span": "model", ".composer-effort-control > span": "effort", "#inspector .inspector-head h2": "workspace", "#goal-bar .eyebrow": "goal", "#goal-clear": "clear", "#goal-edit-cancel": "cancel", "#goal-edit-form .primary-small": "save", "#goal-input": "goalInput", "#composer-live-state > span": "ready", "#terminal-eyebrow": "terminalRemoteShell", "#terminal-title": "terminalTitle", "#terminal-cwd": "terminalNotConnected", "#terminal-hint": "terminalHint", "#terminal-reconnect": "terminalReconnect", "#terminal-screen": "terminalOutput" };
  for (const [selector, key] of Object.entries(text)) { const node = $(selector); if (!node) continue; if (["#message-input", "#session-search"].includes(selector)) node.placeholder = t(key); else if (selector === "#terminal-screen") node.setAttribute("aria-label", t(key)); else node.textContent = t(key); }
  const titles = { "#open-terminal": "terminal", "#sync-native": "sync", "#conversation-rename": "rename", "#conversation-fork": "fork", "#conversation-more": "more", "#language-select": "language", "#goal-edit": "editGoal", "#inspector": "workspace", "#composer-meta": "session", "#terminal-close": "terminalClose", ".shortcut-hint:nth-child(1)": "shortcutTerminal", ".shortcut-hint:nth-child(2)": "shortcutNext", ".shortcut-hint:nth-child(3)": "shortcutPrevious", ".shortcut-hint:nth-child(4)": "shortcutFirst", ".shortcut-hint:nth-child(5)": "shortcutComposerToggle" };
  for (const [selector, key] of Object.entries(titles)) { const node = $(selector); if (node) { node.title = t(key); node.setAttribute("aria-label", t(key)); } }
  const select = $("#language-select"); if (select) select.value = state.language;
  updateThemeToggle();
  updateSessionSidebarControls();
  updateSSHLoginDialog();
  window.dispatchEvent(new CustomEvent("languagechange"));
}
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value = "") => String(value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;", "'":"&#39;"}[c]));
applyLanguage();
function setInspectorOpen(open) {
  const inspector = $("#inspector");
  if (!inspector) return;
  inspector.classList.toggle("open", open);
  inspector.classList.toggle("closed", !open);
  inspector.setAttribute("aria-hidden", open ? "false" : "true");
}
function sessionSidebarIsOpen() {
  const sidebar = $(".session-sidebar");
  if (!sidebar) return false;
  return window.innerWidth <= 860 ? sidebar.classList.contains("open") : !$(".workspace-app").classList.contains("sidebar-collapsed");
}
function updateSessionSidebarControls() {
  const open = sessionSidebarIsOpen();
  const toggle = $("#sidebar-toggle");
  if (toggle) {
    const label = uiLabel(open ? "closeSessions" : "openSessions");
    toggle.title = label; toggle.setAttribute("aria-label", label); toggle.setAttribute("aria-expanded", String(open));
  }
  const close = $("#sidebar-close");
  if (close) { close.title = uiLabel("closeSessions"); close.setAttribute("aria-label", uiLabel("closeSessions")); }
  $(".session-sidebar")?.setAttribute("aria-hidden", open ? "false" : "true");
}
function setSessionSidebarOpen(open, persist = true) {
  const sidebar = $(".session-sidebar");
  const workspace = $(".workspace-app");
  if (!sidebar || !workspace) return;
  if (window.innerWidth <= 860) {
    sidebar.classList.toggle("open", open);
    workspace.classList.remove("sidebar-collapsed");
  } else {
    sidebar.classList.remove("open");
    workspace.classList.toggle("sidebar-collapsed", !open);
    if (persist) localStorage.setItem("codex-partner-sidebar-collapsed", open ? "0" : "1");
  }
  updateSessionSidebarControls();
}
function toggleSessionSidebar() { setSessionSidebarOpen(!sessionSidebarIsOpen()); }
function restoreSessionSidebar() {
  setSessionSidebarOpen(window.innerWidth <= 860 ? false : localStorage.getItem("codex-partner-sidebar-collapsed") !== "1", false);
}
let socket = null;
let socketTaskId = null;
let socketReconnectTimer = null;
let socketPaused = false;
let overviewSocket = null;
let overviewReconnectTimer = null;
let chatRenderFrame = null;
let pendingAttachments = [];
let terminalSocket = null;
let terminalTaskId = null;
let terminalEmulator = null;
let terminalFitAddon = null;
let terminalResizeObserver = null;
let terminalReconnectTimer = null;
let terminalReconnectAttempt = 0;
let terminalShouldReconnect = false;
let queueSyncTimer = null;
let queueSyncInFlight = false;
let taskStatusSyncInFlight = false;
let markdownComponents = [];
const livePhases = new Map();
const liveStartedAt = new Map();
const realtimeChannels = { overview: "connecting", task: "idle", terminal: "idle" };
const realtimeLastSeen = { overview: 0, task: 0, terminal: 0 };
let authPromptPromise = null;

function showAppDialog(options = {}) {
  const dialog = $("#app-dialog");
  const form = $("#app-dialog-form");
  const input = $("#app-dialog-input");
  const field = $("#app-dialog-field");
  $("#app-dialog-title").textContent = options.title || uiLabel("confirmTitle");
  $("#app-dialog-message").textContent = options.message || "";
  $("#app-dialog-label").textContent = options.label || "";
  $("#app-dialog-cancel").textContent = options.cancelLabel || uiLabel("cancel");
  $("#app-dialog-confirm").textContent = options.confirmLabel || uiLabel("confirmAction");
  $("#app-dialog-confirm").className = options.danger ? "danger" : "primary";
  field.hidden = !options.input;
  input.type = options.inputType || "text";
  input.value = options.value || "";
  input.placeholder = options.placeholder || "";
  input.required = Boolean(options.required);
  return new Promise(resolve => {
    let settled = false;
    const finish = value => {
      if (settled) return;
      settled = true;
      dialog.close();
      resolve(value);
    };
    form.onsubmit = event => { event.preventDefault(); if (!form.reportValidity()) return; finish(options.input ? input.value : true); };
    $("#app-dialog-cancel").onclick = () => finish(options.input ? null : false);
    dialog.oncancel = event => { event.preventDefault(); finish(options.input ? null : false); };
    dialog.onclick = event => {
      if (event.target !== dialog) return;
      const bounds = form.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) finish(options.input ? null : false);
    };
    if (dialog.open) dialog.close();
    dialog.showModal();
    requestAnimationFrame(() => (options.input ? input : $("#app-dialog-confirm")).focus());
  });
}

function appConfirm(message, options = {}) {
  return showAppDialog({ ...options, message, input: false });
}

function appPrompt(options = {}) {
  return showAppDialog({ ...options, input: true });
}

function updateSSHLoginDialog() {
  if (!document.querySelector("#ssh-login-dialog")) return;
  $("#ssh-login-title").textContent = uiLabel("authTitle");
  $("#ssh-login-message").textContent = uiLabel("authMessage");
  $("#ssh-login-username-label").textContent = uiLabel("sshUsername");
  $("#ssh-login-password-label").textContent = uiLabel("sshPassword");
  $("#ssh-login-submit").textContent = uiLabel("connect");
}

async function requestSSHLogin() {
  if (authPromptPromise) return authPromptPromise;
  authPromptPromise = new Promise(resolve => {
    const dialog = $("#ssh-login-dialog");
    const form = $("#ssh-login-form");
    const username = $("#ssh-login-username");
    const password = $("#ssh-login-password");
    const error = $("#ssh-login-error");
    const submit = $("#ssh-login-submit");
    updateSSHLoginDialog();
    $("#ssh-login-server").textContent = `SSH · ${location.hostname}:22`;
    fetch("/api/auth/status").then(response => response.json()).then(status => {
      if (status?.ssh_host) $("#ssh-login-server").textContent = `SSH · ${status.ssh_host}:${status.ssh_port || 22}`;
    }).catch(() => {});
    error.textContent = "";
    password.value = "";
    dialog.oncancel = event => event.preventDefault();
    form.onsubmit = async event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      submit.disabled = true;
      error.textContent = "";
      try {
        const response = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: username.value.trim(), password: password.value }),
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw Error(payload.detail || uiLabel("sshLoginFailed"));
        password.value = "";
        dialog.close();
        resolve(true);
      } catch (loginError) {
        error.textContent = loginError.message || uiLabel("sshLoginFailed");
        password.focus();
      } finally {
        submit.disabled = false;
      }
    };
    if (!dialog.open) dialog.showModal();
    requestAnimationFrame(() => (username.value ? password : username).focus());
  }).finally(() => { authPromptPromise = null; });
  return authPromptPromise;
}

async function api(path, options = {}, canPrompt = true) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const response = await fetch(`/api${path}`, { ...options, headers });
  if (response.status === 401 && canPrompt) {
    if (await requestSSHLogin()) return api(path, options, false);
  }
  if (!response.ok) throw Error(await responseErrorMessage(response));
  return response.json();
}
async function responseErrorMessage(response) {
  const text = await response.text().catch(() => "");
  try {
    const payload = JSON.parse(text);
    if (payload?.detail) return String(payload.detail);
  } catch (_) { /* Non-JSON server and proxy errors are expected here. */ }
  const plain = text.trim();
  if (plain && !plain.startsWith("<") && plain.length <= 500) return plain;
  return `${response.status} ${response.statusText || "Request failed"}`.trim();
}
function toast(message) { const node = $("#toast"); node.textContent = message; node.classList.add("show"); setTimeout(() => node.classList.remove("show"), 2600); }
async function copyText(value) {
  try { await navigator.clipboard.writeText(value); }
  catch (_) {
    const input = document.createElement("textarea"); input.value = value; input.style.position = "fixed"; input.style.opacity = "0"; document.body.append(input); input.select(); document.execCommand("copy"); input.remove();
  }
}
function formatBytes(value) {
  let size = Math.max(0, Number(value) || 0); const units = ["B", "KB", "MB", "GB"]; let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  const digits = index === 0 || size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(digits)} ${units[index]}`;
}
function renderCodexAvailability(health) {
  state.codexAvailable = health.codex_available !== false;
  state.codexInstall = health.codex_install || null;
  state.serverInfo = { ...state.serverInfo, ...health, user: health.server_user || "", hostname: health.server_hostname || "" };
  document.body.classList.toggle("codex-missing", !state.codexAvailable);
  const notice = $("#codex-install-notice");
  notice.hidden = state.codexAvailable;
  if (state.codexAvailable) return;
  const plan = state.codexInstall || {};
  $("#codex-install-command").textContent = plan.command || plan.reason || "请在服务器安装 Codex CLI";
  $("#codex-install-command").title = plan.command || plan.reason || "";
  const button = $("#install-codex");
  button.disabled = !plan.supported;
  button.textContent = plan.supported ? "一键安装" : "缺少 npm";
}
function providerUsable(provider) {
  if (!provider || provider.enabled === false) return false;
  if (["auth_error", "unavailable", "invalid", "degraded", "error"].includes(provider.health_status)) return false;
  return Boolean(provider.has_key || provider.native || ["healthy", "reachable", "configured"].includes(provider.health_status));
}
function currentProvider() {
  const task = state.selectedTask;
  const session = task?.active_session_id ? state.sessions.find(item => item.id === task.active_session_id) : null;
  const providerId = session?.provider_id || task?.provider_id;
  if (providerId) return state.providers.find(item => item.id === providerId) || { id: providerId, name: providerId };
  return state.providers.find(item => Number(item.in_use_count || 0) > 0)
    || state.providers.find(item => item.is_default && providerUsable(item))
    || null;
}
function formatMetricMs(value) {
  if (!Number.isFinite(Number(value))) return "--";
  const numeric = Math.max(0, Number(value));
  return numeric < 1000 ? `${Math.round(numeric)}ms` : `${(numeric / 1000).toFixed(1)}s`;
}
function eventTimestampMs(event) {
  const raw = event?.ts ?? event?.created_at;
  if (raw === null || raw === undefined || raw === "") return 0;
  const numeric = Number(raw);
  if (Number.isFinite(numeric)) return numeric < 1e12 ? numeric * 1000 : numeric;
  const parsed = Date.parse(String(raw));
  return Number.isFinite(parsed) ? parsed : 0;
}
function estimateOutputTokens(text) {
  const value = String(text || "");
  const nonAscii = (value.match(/[^\x00-\x7F]/g) || []).length;
  const asciiWords = (value.match(/[A-Za-z0-9_]+/g) || []).reduce((total, word) => total + Math.max(1, Math.ceil(word.length / 4)), 0);
  const punctuation = (value.match(/[.,!?;:{}()[\]`~@#$%^&*+=/\\<>|_-]/g) || []).length;
  return nonAscii + asciiWords + punctuation;
}
function calculateRuntimeMetrics(task = state.selectedTask, events = state.selectedEvents) {
  if (!task) return { taskId: "", ttftMs: null, tpotMs: null, estimated: true, outputTokens: 0 };
  const rows = (events || [])
    .slice(-2000)
    .map(event => ({ event, payload: eventValue(event.payload) || {}, timestamp: eventTimestampMs(event) }))
    .filter(row => row.timestamp)
    .slice()
    .sort((a, b) => a.timestamp - b.timestamp);
  const turnId = row => String(row.payload.turn_id || row.payload.turnId || row.payload?.params?.turnId || "");
  const turnOrder = [];
  for (const row of rows) { const id = turnId(row); if (id && !turnOrder.includes(id)) turnOrder.push(id); }
  let selectedRows = [];
  for (const id of turnOrder.slice().reverse()) {
    const candidate = rows.filter(row => turnId(row) === id);
    if (candidate.some(row => ["agent_delta", "agentmessage"].includes(String(row.payload.type || "").toLowerCase()))) { selectedRows = candidate; break; }
  }
  if (!selectedRows.length) return { taskId: task.id, ttftMs: null, tpotMs: null, estimated: true, outputTokens: 0 };
  const startRow = selectedRows.find(row => ["usermessage", "browsermessage", "externalturnstarted"].includes(String(row.payload.type || "").toLowerCase()));
  const allDeltas = selectedRows.filter(row => String(row.payload.type || "").toLowerCase() === "agent_delta" && String(row.payload.delta || ""));
  const messages = selectedRows.filter(row => String(row.payload.type || "").toLowerCase() === "agentmessage" && String(row.payload.text || row.payload.message || ""));
  const firstOutput = allDeltas[0] || messages[0];
  if (!startRow || !firstOutput) return { taskId: task.id, ttftMs: null, tpotMs: null, estimated: true, outputTokens: 0 };
  const lastItemId = allDeltas[allDeltas.length - 1]?.payload.item_id || "";
  const deltas = lastItemId ? allDeltas.filter(row => row.payload.item_id === lastItemId) : allDeltas;
  const finalMessage = messages[messages.length - 1];
  const outputText = finalMessage
    ? String(finalMessage.payload.text || finalMessage.payload.message || "")
    : deltas.map(row => String(row.payload.delta || "")).join("");
  const outputTokens = estimateOutputTokens(outputText);
  const lastOutput = deltas[deltas.length - 1] || firstOutput;
  const outputElapsed = deltas.length > 1 ? Math.max(0, lastOutput.timestamp - firstOutput.timestamp) : 0;
  return {
    taskId: task.id,
    ttftMs: Math.max(0, firstOutput.timestamp - startRow.timestamp),
    tpotMs: deltas.length > 1 && outputTokens > 1 ? outputElapsed / (outputTokens - 1) : null,
    estimated: true,
    outputTokens,
  };
}
function calculateContextUsage(events = state.selectedEvents) {
  for (const event of (events || []).slice().reverse()) {
    const payload = eventValue(event.payload) || {};
    if (payload.type !== "codex" || payload.method !== "thread/tokenUsage/updated") continue;
    const usage = payload.params?.tokenUsage || {};
    const used = Number(usage.last?.inputTokens);
    const capacity = Number(usage.modelContextWindow);
    if (!Number.isFinite(used) || !Number.isFinite(capacity) || capacity <= 0) continue;
    const boundedUsed = Math.min(Math.max(0, used), capacity);
    return { used: boundedUsed, capacity, remaining: Math.max(0, capacity - boundedUsed), usedPercent: boundedUsed / capacity * 100 };
  }
  return null;
}
function formatCompactTokens(value) {
  const numeric = Math.max(0, Number(value) || 0);
  if (numeric >= 1_000_000) return `${(numeric / 1_000_000).toFixed(numeric >= 10_000_000 ? 0 : 1)}M`;
  if (numeric >= 1000) return `${Math.round(numeric / 1000)}K`;
  return String(Math.round(numeric));
}
function renderContextUsage() {
  const node = $("#composer-context-usage");
  if (!node) return;
  const usage = calculateContextUsage();
  const copy = node.querySelector(".context-usage-copy");
  const fill = node.querySelector(".context-usage-track i");
  node.classList.remove("warning", "critical");
  if (!usage) {
    copy.textContent = "Context --";
    fill.style.width = "0%";
    node.title = uiLabel("contextUsage");
    return;
  }
  const remainingPercent = Math.max(0, Math.round(100 - usage.usedPercent));
  copy.textContent = `${uiLabel("contextRemaining")} ${formatCompactTokens(usage.remaining)} · ${remainingPercent}%`;
  fill.style.width = `${Math.min(100, usage.usedPercent).toFixed(1)}%`;
  node.classList.toggle("warning", remainingPercent <= 35);
  node.classList.toggle("critical", remainingPercent <= 15);
  node.title = `${uiLabel("contextUsage")} · ${formatCompactTokens(usage.used)} / ${formatCompactTokens(usage.capacity)}`;
}
function renderConnectionStatus() {
  const node = $("#connection-status");
  if (!node) return;
  const terminalOpen = $("#terminal-window")?.classList.contains("open");
  const channels = [realtimeChannels.overview];
  if (state.selectedId && realtimeChannels.task !== "paused") channels.push(realtimeChannels.task);
  if (terminalOpen && !["idle", "closed"].includes(realtimeChannels.terminal)) channels.push(realtimeChannels.terminal);
  let kind = "live"; let label = uiLabel("realtime");
  if (!navigator.onLine) { kind = "offline"; label = uiLabel("disconnected"); }
  else if (channels.some(value => ["reconnecting", "offline"].includes(value))) { kind = "reconnecting"; label = uiLabel("reconnecting"); }
  else if (channels.some(value => ["connecting", "idle"].includes(value))) { kind = "connecting"; label = uiLabel("connecting"); }
  const overlayOpen = $("#panel")?.classList.contains("open") || $("#drawer")?.classList.contains("open");
  node.className = `connection-popover ${kind}${overlayOpen ? " panel-open" : ""}`;
  $("#connection-status-label").textContent = label;
  const serverLabel = $("#connection-server-label");
  const webHost = location.hostname || state.serverInfo.hostname || "网页服务器";
  const webPort = location.port || "8787";
  const serverName = `${state.serverInfo.user || "服务用户"}@${webHost}`;
  if (serverLabel) { serverLabel.textContent = webHost; serverLabel.title = `${serverName}:${webPort}`; }
  const provider = currentProvider();
  const metrics = state.runtimeMetrics || {};
  const providerLabel = $("#connection-provider-label");
  const metricsLabel = $("#connection-metrics-label");
  const currentApiLabel = state.language === "zh" ? "当前 API ·" : `${uiLabel("currentApi")} ·`;
  if (providerLabel) providerLabel.textContent = `${currentApiLabel} ${provider?.name || uiLabel("automatic")}`;
  if (metricsLabel) metricsLabel.textContent = `TTFT ${formatMetricMs(metrics.ttftMs)} · TPOT ${formatMetricMs(metrics.tpotMs)}${metrics.estimated && metrics.outputTokens ? ` · ${uiLabel("estimated")}` : ""}`;
  const names = { overview: "总览", task: "会话", terminal: "终端" };
  const values = { connecting: "连接中", live: "实时", reconnecting: "重连中", offline: "中断", idle: "未启用", closed: "已退出", paused: "已暂停" };
  const serverDetail = `${state.serverInfo.user || "服务用户"}@${webHost}:${webPort} · 网页服务已连接`;
  node.title = `${Object.entries(realtimeChannels).map(([key, value]) => `${names[key]}：${values[value] || value}`).join(" · ")} · 服务：${serverDetail} · 当前 API：${provider?.name || "自动选择"} · TTFT ${formatMetricMs(metrics.ttftMs)} · TPOT ${formatMetricMs(metrics.tpotMs)} · 点击查看网页服务`;
}
function setRealtimeChannel(channel, value) {
  if (realtimeChannels[channel] === value) return;
  realtimeChannels[channel] = value;
  renderConnectionStatus();
}
function noteRealtime(channel) { realtimeLastSeen[channel] = Date.now(); }
function heartbeatRealtime() {
  const entries = [["overview", overviewSocket], ["task", socket], ["terminal", terminalSocket]];
  for (const [channel, current] of entries) {
    if (!current || current.readyState !== WebSocket.OPEN) continue;
    if (realtimeLastSeen[channel] && Date.now() - realtimeLastSeen[channel] > 32_000) {
      setRealtimeChannel(channel, "reconnecting");
      current.close(4000, "heartbeat timeout");
      continue;
    }
    try { current.send(JSON.stringify({ type: "ping" })); } catch (_) { current.close(); }
  }
}
function statusLabel(value) { const keys = { running: "statusRunning", retrying: "statusRetrying", queued: "statusQueued", available: "statusAvailable", succeeded: "statusSucceeded", failed: "statusFailed", stopped: "statusStopped", archived: "statusArchived", trashed: "statusTrashed", imported: "statusImported" }; return keys[value] ? t(keys[value]) : value || t("unknown"); }
function status(value) { return `<span class="status status-${esc(value)}"><i></i>${esc(statusLabel(value))}</span>`; }
function shortDate(value) {
  if (!value) return "";
  const date = new Date(value); if (Number.isNaN(date.valueOf())) return value;
  const delta = Date.now() - date.valueOf();
  const locale = state.language === "zh" ? "zh-CN" : state.language;
  const relative = new Intl.RelativeTimeFormat(locale, { numeric: "auto", style: "short" });
  if (delta < 60_000) return relative.format(0, "second");
  if (delta < 3_600_000) return relative.format(-Math.floor(delta / 60_000), "minute");
  if (delta < 86_400_000) return relative.format(-Math.floor(delta / 3_600_000), "hour");
  return date.toLocaleDateString(locale, { month: "short", day: "numeric" });
}
function eventValue(value) { try { return typeof value === "string" ? JSON.parse(value) : value; } catch (_) { return value; } }
function eventText(value) {
  const payload = eventValue(value);
  if (typeof payload === "string") return payload;
  if (!payload || typeof payload !== "object") return String(payload ?? "");
  if (payload.type === "userMessage" || payload.type === "browserMessage") return payload.text || (payload.content || []).map(x => x.text || "").join("\n");
  if (payload.type === "agentMessage") return payload.text || "";
  if (payload.type === "agentMessageStarted") return "";
  if (payload.type === "agent_delta") return payload.delta || "";
  if (payload.type === "reasoning") return payload.text || payload.summary || "";
  if (payload.type === "fileChange") return payload.text || uiLabel("fileChanged");
  if (payload.type === "contextCompaction") return uiLabel("contextCompressed");
  if (payload.type === "externalTurnStarted") return uiLabel("terminalTurnStarted");
  if (payload.type === "turn_completed") return uiLabel("terminalTurnCompleted");
  if (payload.type === "turn_aborted") return uiLabel("terminalTurnAborted");
  if (payload.type === "provider_failover") return uiLabel("providerSwitched", { from: payload.from || uiLabel("default"), to: payload.to || uiLabel("nextProvider") });
  if (payload.type === "slashCommand" || payload.type === "commandResult") return payload.text || "";
  if (payload.type === "codex") return payload.method ? `Codex · ${payload.method}` : uiLabel("codexActivity");
  if (payload.delta) return payload.delta;
  return payload.text || JSON.stringify(payload);
}
function activityPhase(value) {
  const payload = eventValue(value);
  if (!payload || typeof payload !== "object") return "";
  const type = String(payload.type || payload.item?.type || "").toLowerCase();
  const method = String(payload.method || "").toLowerCase();
  if (type === "agent_delta" || type.includes("agentmessage")) return "phaseGenerating";
  if (type.includes("reason") || type === "plan") return "phasePlanning";
  if (type.includes("command") || method.includes("command")) return "phaseCommand";
  if (type.includes("file") || method.includes("file")) return "phaseFiles";
  if (type.includes("tool") || type.includes("mcp") || method.includes("tool")) return "phaseTools";
  if (type.includes("search") || method.includes("search")) return "phaseSearch";
  if (type === "contextcompaction" || method.includes("compact")) return "phaseCompact";
  if (type === "provider_failover") return "phaseProvider";
  if (type === "externalturnstarted") return "phaseTerminalSynced";
  if (type === "usermessage") return "phaseInstruction";
  return "";
}
function activityPhaseLabel(value) {
  if (UI_LABELS[value]) return uiLabel(value);
  for (const key of Object.keys(UI_LABELS).filter(item => item.startsWith("phase"))) {
    if (Object.values(UI_LABELS[key]).includes(value)) return uiLabel(key);
  }
  return value || uiLabel("phaseAnalyzing");
}
function latestActivityPhase(task) {
  for (let index = state.selectedEvents.length - 1; index >= 0; index -= 1) {
    const event = state.selectedEvents[index];
    if (task?.active_session_id && event.session_id !== task.active_session_id) continue;
    const phase = activityPhase(event.payload);
    if (phase) return phase;
  }
  return "";
}
function activeStartedAt(task) {
  if (task.external_started_at) {
    const external = new Date(task.external_started_at).valueOf();
    const started = Number.isFinite(external) ? external : Date.now();
    liveStartedAt.set(task.id, started);
    return started;
  }
  if (liveStartedAt.has(task.id)) return liveStartedAt.get(task.id);
  const sessions = task.sessions || state.sessions.filter(session => session.task_id === task.id);
  const active = sessions.find(session => session.id === task.active_session_id) || sessions.find(session => ["running", "retrying"].includes(session.status));
  const candidate = new Date(active?.started_at || task.updated_at || Date.now()).valueOf();
  const started = Number.isFinite(candidate) ? candidate : Date.now();
  liveStartedAt.set(task.id, started);
  return started;
}
function elapsedLabel(started) {
  const total = Math.max(0, Math.floor((Date.now() - started) / 1000));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}
function updateTurnElapsed() {
  const task = state.selectedTask;
  const tip = $("#turn-progress-tip");
  if (!tip || !task || !["running", "retrying", "queued"].includes(task.status)) return;
  activeStartedAt(task);
  const next = activityPhaseLabel(livePhases.get(task.id) || task.external_phase || "phaseAnalyzing");
  if (tip.textContent !== next) tip.textContent = next;
}
function renderTurnProgress() {
  const task = state.selectedTask;
  const node = $("#turn-progress");
  if (!node || !task) return;
  const active = ["running", "retrying", "queued"].includes(task.status);
  node.className = `turn-progress ${active ? "active" : "idle"} ${esc(task.status || "available")}`;
  node.setAttribute("aria-hidden", "false");
  if (!active) {
    livePhases.delete(task.id);
    liveStartedAt.delete(task.id);
    $("#turn-progress-tip").textContent = standbyTipLabel();
    node.setAttribute("aria-label", uiLabel("ready"));
    return;
  }
  if (!livePhases.has(task.id)) livePhases.set(task.id, task.external_phase || latestActivityPhase(task) || (task.status === "queued" ? "phaseQueued" : "phaseAnalyzing"));
  node.setAttribute("aria-label", task.status === "retrying" ? uiLabel("recoveringTitle") : uiLabel("workingTitle"));
  updateTurnElapsed();
}
async function reconcileSelectedTaskStatus() {
  const current = state.selectedTask;
  if (!current || !["running", "retrying", "queued"].includes(current.status) || taskStatusSyncInFlight) return;
  const taskId = current.id;
  taskStatusSyncInFlight = true;
  try {
    const authoritative = await api(`/tasks/${encodeURIComponent(taskId)}`);
    if (state.selectedId !== taskId || !state.selectedTask) return;
    const changed = authoritative.status !== state.selectedTask.status
      || authoritative.active_session_id !== state.selectedTask.active_session_id
      || authoritative.run_mode !== state.selectedTask.run_mode
      || authoritative.updated_at !== state.selectedTask.updated_at;
    state.selectedTask = { ...state.selectedTask, ...authoritative };
    mergeTask(authoritative);
    if (changed) { renderConversation(); renderSessionList(); renderSidebarStats(); }
  } catch (_) {
    // Realtime reconnect handling remains authoritative while the server is unavailable.
  } finally {
    taskStatusSyncInFlight = false;
  }
}
function isUserEvent(event) { const payload = eventValue(event.payload); return payload?.type === "userMessage" || payload?.type === "browserMessage" || payload?.type === "slashCommand" || event.stream === "user"; }
function isAssistantEvent(event) { const payload = eventValue(event.payload); return payload?.type === "agentMessage" || payload?.type === "agent_delta" || payload?.type === "commandResult" || event.stream === "assistant"; }
function taskMatches(task) { if (state.filter === "running" && !["running", "retrying", "queued"].includes(task.status)) return false; if (state.filter !== "all" && state.filter !== "running" && task.status !== state.filter) return false; if (!state.query) return true; const haystack = `${task.name} ${task.prompt} ${task.goal} ${task.workspace}`.toLowerCase(); return haystack.includes(state.query.toLowerCase()); }
function taskIsRunningGroup(task) { return ["running", "retrying", "queued"].includes(task?.status); }
function taskSortName(task) { return String(task?.name || task?.prompt || task?.id || "").trim().toLocaleLowerCase() || String(task?.id || ""); }
function taskSortTime(task) { return String(task?.updated_at || task?.created_at || ""); }
function sortTasks(tasks) {
  return [...tasks].sort((a, b) => {
    const activeDelta = Number(taskIsRunningGroup(b)) - Number(taskIsRunningGroup(a));
    if (activeDelta) return activeDelta;
    if (taskIsRunningGroup(a) && taskIsRunningGroup(b)) {
      return taskSortName(a).localeCompare(taskSortName(b), undefined, { numeric: true, sensitivity: "base" }) || String(a.id || "").localeCompare(String(b.id || ""));
    }
    return taskSortTime(b).localeCompare(taskSortTime(a)) || String(b.id || "").localeCompare(String(a.id || ""));
  });
}
function mergeTask(task) {
  const index = state.tasks.findIndex(item => item.id === task.id);
  if (index < 0) state.tasks = [task, ...state.tasks];
  else state.tasks[index] = { ...state.tasks[index], ...task };
}
function mergeOverviewSnapshot(tasks) {
  const incoming = new Map(tasks.map(task => [task.id, task]));
  state.tasks = state.tasks.map(task => incoming.get(task.id) ? { ...task, ...incoming.get(task.id) } : task);
  for (const task of tasks) if (!state.tasks.some(item => item.id === task.id)) state.tasks.push(task);
}
function normalizeTaskMessage(message, current = null) {
  const id = message.id || message.message_id;
  if (!id) return null;
  const ranks = { sending: 0, queued: 1, dispatching: 2, running: 3, steering: 3, sent: 4, steered: 4, failed: 4 };
  const incomingRank = ranks[message.status];
  const currentRank = ranks[current?.status];
  // A fast dispatch can finish over WebSocket before the original enqueue
  // request returns. Never let that older HTTP response resurrect the row.
  if (current && !message.error && incomingRank !== undefined && currentRank !== undefined && incomingRank < currentRank) {
    message = { ...message, status: current.status, started_at: current.started_at, finished_at: current.finished_at, session_id: current.session_id, error: current.error || "" };
  }
  return { id, ...(current || {}), ...message, body: message.body ?? current?.body ?? "", _optimistic: message._optimistic === true };
}
function upsertTaskMessage(message) {
  const id = message.id || message.message_id;
  if (!id) return;
  const index = state.selectedMessages.findIndex(item => item.id === id);
  const normalized = normalizeTaskMessage(message, state.selectedMessages[index]);
  if (index < 0) state.selectedMessages.push(normalized);
  else state.selectedMessages[index] = normalized;
}
function replaceTaskMessages(messages) {
  const current = new Map(state.selectedMessages.map(message => [message.id, message]));
  const authoritative = (messages || []).map(message => normalizeTaskMessage(message, current.get(message.id || message.message_id))).filter(Boolean);
  const authoritativeIds = new Set(authoritative.map(message => message.id));
  const pending = state.selectedMessages.filter(message => message._optimistic === true && !authoritativeIds.has(message.id));
  state.selectedMessages = [...authoritative, ...pending].sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")) || String(a.id).localeCompare(String(b.id)));
}
function removeTaskMessage(messageId) {
  state.selectedMessages = state.selectedMessages.filter(message => message.id !== messageId);
}
function refreshComposerHistory() {
  const seen = new Set();
  state.composerHistory = [...(state.selectedMessages || [])]
    .filter(message => message.body && ["sent", "steered", "failed", "running", "steering", "sending"].includes(message.status))
    .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")))
    .map(message => message.body)
    .filter(body => !seen.has(body) && seen.add(body))
    .slice(-40);
  state.historyIndex = -1;
}
function scheduleRenderChat() {
  if (state.chatSelectionActive) { state.chatRenderDeferred = true; return; }
  if (chatRenderFrame) return;
  chatRenderFrame = requestAnimationFrame(() => { chatRenderFrame = null; renderChat(); });
}

async function refresh(keepSelection = true) {
  try {
    const [tasks, sessions, providers, skills, memories, health, commandData] = await Promise.all([api("/tasks"), api("/sessions"), api("/providers"), api("/skills"), api("/memories"), api("/health"), api("/commands"), loadUserProfile()]);
    state.tasks = tasks; state.sessions = sessions; state.providers = providers; state.skills = skills; state.memories = memories.files || []; state.generatedMemories = memories.generated || []; state.commands = commandData.commands || []; state.defaultWorkspace = health.default_workspace || state.defaultWorkspace; renderCodexAvailability(health); renderConnectionStatus();
    renderSessionList(); renderSidebarStats(); refreshOpenPanel();
    if (state.selectedId && !state.tasks.some(task => task.id === state.selectedId)) state.selectedId = null;
    if (!overviewSocket || overviewSocket.readyState === WebSocket.CLOSED) connectOverviewSocket();
    if (!state.selectedId && state.tasks.length) await selectSession(sortTasks(state.tasks)[0].id);
    else if (state.selectedId && (keepSelection || !state.selectedTask)) await selectSession(state.selectedId, !state.selectedTask);
  } catch (error) { setRealtimeChannel("overview", "reconnecting"); connectOverviewSocket(); toast(error.message); }
}
async function refreshSelectedConversation(id) {
  try {
    const [task, timeline, messages] = await Promise.all([api(`/tasks/${id}`), api(`/tasks/${id}/timeline?limit=160`), api(`/tasks/${id}/messages`)]);
    if (state.selectedId !== id) return;
    if (task.id && task.id !== id) {
      state.selectedId = task.id;
      localStorage.setItem("codex-dashboard-session", task.id);
      if (!socketPaused) connectSocket(task.id, true);
    }
    mergeTask(task);
    state.selectedTask = task;
    state.selectedEvents = [...(timeline.items || []), ...(timeline.metrics || [])];
    state.historyCursor = timeline.next_cursor || ""; state.historyHasMore = Boolean(timeline.has_more);
    state.chatVirtualStart = null;
    replaceTaskMessages(messages);
    renderConversation(); renderSessionList(); renderSidebarStats();
  } catch (_) { /* Reconnect or the overview channel will retry authoritative state. */ }
}
