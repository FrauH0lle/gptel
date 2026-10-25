;;; stack-check.el --- Trusted isolated stack validation -*- lexical-binding: t; -*-

;; Load this file from the control checkout, NEVER from the candidate checkout.
;; This is not a sandbox: the containing job must have no secrets or write token.

(require 'cl-lib)
(require 'json)
(require 'package)

(defvar stack-check-config
  (json-parse-string
   (with-temp-buffer
     (insert-file-contents (getenv "STACK_VALIDATE_CONFIG")) (buffer-string))
   :object-type 'alist :array-type 'list))
(defvar stack-check-result nil)

(defun stack-check-get (key)
  (alist-get key stack-check-config))

(defun stack-check-put (key value)
  (setf (alist-get key stack-check-result) value))

(defun stack-check-write ()
  (with-temp-file (getenv "STACK_VALIDATE_RESULT")
    (insert (json-encode stack-check-result))))

(setq user-emacs-directory (expand-file-name ".emacs.d/" (getenv "HOME"))
      package-user-dir (stack-check-get 'packages)
      package-directory-list nil
      package-enable-at-startup nil
      package-quickstart nil
      package-native-compile nil
      native-comp-jit-compilation nil
      enable-local-variables nil
      enable-local-eval nil
      auth-sources nil
      url-privacy-level 'high
      gnutls-verify-error t
      load-prefer-newer t)

(defun stack-check-package-metadata ()
  "Record installed versions and actual loaded files, not just requested minima."
  (vconcat
   (mapcar
    (lambda (entry)
      (let* ((desc (cadr entry))
             (directory (file-name-as-directory (package-desc-dir desc)))
             (loaded (cl-loop for (path . _) in load-history
                              when (and path (file-in-directory-p path directory))
                              collect path)))
        `((name . ,(symbol-name (car entry)))
          (version . ,(package-version-join (package-desc-version desc)))
          (archive . ,(package-desc-archive desc))
          (directory . ,directory)
          (library . ,(locate-library (symbol-name (car entry))))
          (loaded_paths . ,(vconcat loaded)))))
    package-alist)))

(defun stack-check-dependencies ()
  (unless (string= emacs-version "31.1")
    (error "Validation requires Emacs 31.1, found %s" emacs-version))
  ;; Stable GNU packages can depend on stable NonGNU packages (for example,
  ;; Transient 0.13.8 requires cond-let and llama).  Do not use MELPA/devel.
  (setq package-archives '(("gnu" . "https://elpa.gnu.org/packages/")
                           ("nongnu" . "https://elpa.nongnu.org/nongnu/"))
        package-archive-priorities '(("gnu" . 10) ("nongnu" . 0)))
  (package-initialize)
  (let ((requirements
         (with-temp-buffer
           (insert-file-contents (expand-file-name "gptel.el" (stack-check-get 'tree)))
           (package-desc-reqs (package-buffer-info)))))
    (unless requirements (error "Missing Package-Requires declaration"))
    (stack-check-put
     'requirements
     (vconcat (mapcar (lambda (req)
                       `((name . ,(symbol-name (car req)))
                         (minimum . ,(package-version-join (cadr req)))))
                     requirements)))
    ;; The recorded suite exercises Markdown prompts, media, and persistence.
    ;; Without this test-only prerequisite some tests skip or use the wrong mode.
    (stack-check-put 'test_requirements [((name . "markdown-mode") (minimum . "0"))])
    (package-refresh-contents)
    (dolist (req (append requirements '((markdown-mode (0)))))
      (if (eq (car req) 'emacs)
          (unless (version-list-<= (cadr req) (version-to-list emacs-version))
            (error "Emacs does not satisfy Package-Requires"))
        (let ((desc (cadr (assq (car req) package-archive-contents))))
          (unless (and desc (version-list-<= (cadr req) (package-desc-version desc)))
            (error "Stable ELPA archives have no sufficient version for %S" req))
          (package-install desc)
          (unless (package-installed-p (car req) (cadr req))
            (error "Installed dependency does not satisfy %S" req))
          (require (car req)))))
    (stack-check-put 'dependencies (stack-check-package-metadata))))

(defun stack-check-source-paths ()
  (package-initialize)
  (add-to-list 'load-path (stack-check-get 'tree))
  (add-to-list 'load-path (stack-check-get 'tests)))

(defun stack-check-compile ()
  (require 'bytecomp)
  (stack-check-source-paths)
  (make-directory (stack-check-get 'compiled) t)
  (let ((byte-compile-error-on-warn nil)
        (byte-compile-dest-file-function
         (lambda (source)
           (expand-file-name (concat (file-name-nondirectory source) "c")
                             (stack-check-get 'compiled))))
        results failed)
    (dolist (source (stack-check-get 'candidate_files))
      (condition-case err
          (if (byte-compile-file source)
              (push `((file . ,source) (status . "pass")) results)
            (setq failed t)
            (push `((file . ,source) (status . "fail")) results))
        (error
         (setq failed t)
         (push `((file . ,source) (status . "fail")
                 (error . ,(error-message-string err))) results))))
    (stack-check-put 'files (vconcat (nreverse results)))
    (stack-check-put 'dependencies (stack-check-package-metadata))
    (when failed (error "Byte compilation failed; see per-file results and output"))))

(defun stack-check-ert-statistics (stats)
  (stack-check-put 'dependencies (stack-check-package-metadata))
  (stack-check-put 'test_count (ert-stats-total stats))
  (stack-check-put 'completed (ert-stats-completed stats))
  (stack-check-put 'unexpected (ert-stats-completed-unexpected stats))
  (stack-check-put 'skipped (ert-stats-skipped stats))
  (stack-check-put 'status
                   (if (and (> (ert-stats-total stats) 0)
                            (= (ert-stats-total stats) (ert-stats-completed stats))
                            (= (ert-stats-completed-unexpected stats) 0)
                            (= (ert-stats-skipped stats) 0)) "pass" "fail"))
  (stack-check-write)
  stats)

(defun stack-check-ert ()
  (require 'ert)
  (stack-check-source-paths)
  ;; Match the recorded Makefile: all visible top-level candidate .el, then
  ;; all visible top-level test .el (including helpers, not just *-test.el).
  (let ((default-directory (file-name-as-directory (stack-check-get 'tests))))
    (dolist (source (append (stack-check-get 'candidate_files)
                            (stack-check-get 'test_files)))
      (load source nil nil t))
    (let ((count (length (ert-select-tests t t))))
      (stack-check-put 'test_count count)
      (when (zerop count) (error "No ERT tests discovered")))
    (advice-add 'ert-run-tests-batch :filter-return #'stack-check-ert-statistics)
    (ert-run-tests-batch-and-exit t)))

(condition-case err
    (progn
      (stack-check-put 'emacs `((version . ,emacs-version)
                               (executable . ,(expand-file-name invocation-name
                                                               invocation-directory))
                               (system_configuration . ,system-configuration)))
      (pcase (getenv "STACK_VALIDATE_MODE")
        ("dependencies" (stack-check-dependencies))
        ("compile" (stack-check-compile))
        ("ert" (stack-check-ert))
        (_ (error "Unknown validation mode")))
      (stack-check-put 'status "pass")
      (stack-check-write))
  (error
   (stack-check-put 'status "fail")
   (stack-check-put 'error (error-message-string err))
   (stack-check-write)
   (message "%s" (error-message-string err))
   (kill-emacs 1)))

;;; stack-check.el ends here
