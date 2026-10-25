;;; gptel-stream-state-test.el --- Local stream regressions -*- lexical-binding: t; -*-

(require 'ert)
(require 'cl-lib)
(require 'gptel)
(require 'gptel-openai)

(ert-deftest gptel-local-repeated-role-does-not-reopen-reasoning ()
  "A repeated assistant role must not produce duplicate reasoning end markers."
  (let* ((gptel--known-backends nil)
         (backend (gptel-make-openai "Local regression"
                                    :host "localhost" :key "unused"))
         (fsm (gptel-make-fsm))
         (proc (make-process :name "gptel-local-stream-test" :command nil))
         events
         (info (list :backend backend :data (list :messages [])
                     :http-status "200" :status "HTTP/2 200"
                     :callback (lambda (response _info) (push response events)))))
    (setf (gptel-fsm-info fsm) info
          (alist-get proc gptel--request-alist) (list fsm))
    (unwind-protect
        (with-temp-buffer
          (setf (process-buffer proc) (current-buffer))
          (dolist (delta '("{\"role\":\"assistant\",\"reasoning_content\":\"why\"}"
                           "{\"role\":\"assistant\",\"content\":\"A\"}"
                           "{\"role\":\"assistant\",\"content\":\"B\"}"
                           "{\"role\":\"assistant\",\"reasoning_content\":\"late\"}"))
            (gptel-curl--stream-filter
             proc (concat "data: {\"choices\":[{\"delta\":" delta "}]}\n\n")))
          (should (equal (nreverse events)
                         '((reasoning . "why") (reasoning . t) "A" "B")))
          (should (eq (plist-get info :reasoning-block) 'done))
          ;; Preserve late reasoning for tool-call replay, without displaying it
          ;; as another reasoning block in the same response.
          (should (equal (reverse (plist-get info :reasoning-chunks))
                         '(:reasoning_content "why" "late"))))
      (setf (alist-get proc gptel--request-alist nil t) nil)
      (delete-process proc))))

(ert-deftest gptel-local-new-request-resets-reasoning-state ()
  "Tool continuations must start a fresh reasoning block with either transport."
  (dolist (curl '(t nil))
    (with-temp-buffer
      (let* ((info (list :buffer (current-buffer)
                         :reasoning-block 'done :reasoning "previous response"))
             (fsm (gptel-make-fsm :info info))
             (gptel-use-curl curl)
             (gptel-post-request-hook nil)
             called
             (check (lambda (machine)
                      (setq called t)
                      (should (eq machine fsm))
                      (should-not (plist-get (gptel-fsm-info machine) :reasoning))
                      (should-not (plist-get (gptel-fsm-info machine) :reasoning-block)))))
        (cl-letf (((symbol-function 'gptel-curl-get-response) check)
                  ((symbol-function 'gptel--url-get-response) check))
          (gptel--handle-wait fsm))
        (should called)))))

;;; gptel-stream-state-test.el ends here
