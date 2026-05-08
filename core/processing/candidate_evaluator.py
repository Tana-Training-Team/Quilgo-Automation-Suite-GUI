# v2/core/processing/candidate_evaluator.py

import pandas as pd
import json
from .quilgo_parser import MASTER_TEST_CONFIG, ROLE_TO_TEST_MAPPING, SLUG_MAPPING, ROLE_TO_DROPDOWN_OPTION_MAP, ROLE_TO_CATEGORY_MAPPING

def _generate_summary_notes(candidate_eval_data, integrity_df, manual_decisions=None):
    email = candidate_eval_data['email']

    if candidate_eval_data.get('auto_failed_reason'):
        reason = candidate_eval_data['auto_failed_reason']
        summary_note_md = (
            "**Evaluation of Quilgo Assessments**\n\n"
            "**Overall Recommendation:** Drop Candidate\n\n"
            f"**Reason for Automatic Disqualification:**\n{reason}"
        )
        summary_note_html = (
            "<p><strong>Evaluation of Quilgo Assessments</strong></p>"
            "<p><strong>Overall Recommendation:</strong> Drop Candidate</p>"
            f"<p><strong>Reason for Automatic Disqualification:</strong><br>{reason}</p>"
        )
        return summary_note_md, summary_note_html

    roles = candidate_eval_data['roles']
    qualified_roles = [rn for rn, rd in roles.items() if "QUALIFIED" in rd.get('status', '')]
    overall_recommendation = "Advance Candidate" if qualified_roles else "Reject Candidate"

    all_tests_taken = [t for rd in roles.values() for t in rd.get('tests', [])]
    total_tests_done = len(all_tests_taken)
    flagged_tests = integrity_df[integrity_df['email'] == email] if not integrity_df.empty else pd.DataFrame()
    num_flagged_tests = len(flagged_tests)
    clean_percent = ((total_tests_done - num_flagged_tests) / total_tests_done * 100) if total_tests_done > 0 else 100
    passed_tests = [t for t in all_tests_taken if t.get('score', 0) >= 7]
    total_passed_count = len(passed_tests)
    flagged_passed_tests_count = 0
    if total_passed_count > 0 and not integrity_df.empty:
        passed_test_names = [t['name'] for t in passed_tests]
        flagged_passed = integrity_df[(integrity_df['email'] == email) & (integrity_df['test_name'].isin(passed_test_names))]
        flagged_passed_tests_count = len(flagged_passed)
    clean_percent_passed = ((total_passed_count - flagged_passed_tests_count) / total_passed_count * 100) if total_passed_count > 0 else 100

    # Build flagged test lookup for inline annotations in the full breakdown
    flagged_map = {}
    if not flagged_tests.empty:
        for _, fr in flagged_tests.iterrows():
            flagged_map[fr['test_name']] = fr['Issue_Types']

    md_lines = [
        "**Evaluation of Quilgo Assessments**",
        f"**Overall Recommendation:** {overall_recommendation}",
    ]
    html_lines = [
        "<p><strong>Evaluation of Quilgo Assessments</strong></p>",
        f"<p><strong>Overall Recommendation:</strong> {overall_recommendation}</p>",
    ]

    # Justification from manual review decisions
    if manual_decisions:
        justification_parts = [
            f"{d['role']}: {d['justification']}"
            for d in manual_decisions
            if d.get('justification')
        ]
        if justification_parts:
            combined = "; ".join(justification_parts)
            md_lines.append(f"**Justification:** {combined}")
            html_lines.append(f"<p><strong>Justification:</strong> {combined}</p>")

    # Full breakdown — per role, per test with score and status
    md_lines.append("\n**Full Breakdown:**")
    html_lines.append("<p><strong>Full Breakdown:</strong></p><ul>")
    for role_name, role_data in roles.items():
        role_status = role_data.get('status', 'UNKNOWN')
        tests = role_data.get('tests', [])
        md_lines.append(f"  - **{role_name}** — {role_status}")
        html_lines.append(f"<li><strong>{role_name}</strong> — {role_status}<ul>")
        for test in tests:
            tname = test['name']
            tscore = test['score']
            tstatus = test['status']
            flag_note = f" | Flag: {flagged_map[tname]}" if tname in flagged_map else ""
            md_lines.append(f"    - {tname}: {tscore}/10 [{tstatus}]{flag_note}")
            html_lines.append(f"<li>{tname}: {tscore}/10 [{tstatus}]{flag_note}</li>")
        html_lines.append("</ul></li>")
    html_lines.append("</ul>")

    # Integrity flags section
    md_lines.append("\n**Integrity Flags:**")
    html_lines.append("<p><strong>Integrity Flags:</strong></p>")
    if flagged_map:
        html_lines.append("<ul>")
        for tname, issues in flagged_map.items():
            md_lines.append(f"  - {tname}: {issues}")
            html_lines.append(f"<li>{tname}: {issues}</li>")
        html_lines.append("</ul>")
    else:
        md_lines.append("  - None detected")
        html_lines.append("<p>None detected</p>")

    # Summary metrics
    md_lines += [
        "\n**Scores & Summary:**",
        f"- Total Tests Done: {total_tests_done}",
        f"- Overall Integrity Score: {num_flagged_tests}/{total_tests_done} flagged ({clean_percent:.0f}% clean)",
        f"- Passed Tests (score ≥ 7): {total_passed_count}/{total_tests_done}",
        f"- Integrity of Passed Tests: {flagged_passed_tests_count}/{total_passed_count} flagged ({clean_percent_passed:.0f}% clean)",
    ]
    html_lines += [
        "<p><strong>Scores &amp; Summary:</strong></p><ul>",
        f"<li>Total Tests Done: {total_tests_done}</li>",
        f"<li>Overall Integrity Score: {num_flagged_tests}/{total_tests_done} flagged ({clean_percent:.0f}% clean)</li>",
        f"<li>Passed Tests (score ≥ 7): {total_passed_count}/{total_tests_done}</li>",
        f"<li>Integrity of Passed Tests: {flagged_passed_tests_count}/{total_passed_count} flagged ({clean_percent_passed:.0f}% clean)</li>",
        "</ul>",
    ]

    summary_note_md = "\n".join(md_lines)
    summary_note_html = "".join(html_lines)
    return summary_note_md, summary_note_html


def evaluate_and_triage_candidates(manatal_df, quilgo_df, integrity_df, get_manual_review_decision, start_date=None, end_date=None):
    """
    Main evaluation function with iterative manual review logic and a "skip all" feature.

    Args:
        start_date (pd.Timestamp | None): If set, only submissions on or after this date are scored.
        end_date   (pd.Timestamp | None): If set, only submissions on or before this date are scored.
                                          When start_date is given but end_date is omitted it defaults
                                          to today (UTC) so the range is always well-defined.
    """
    print("\n" + "="*60)
    print("🧠 Starting Advanced Candidate Evaluation & Triage (Iterative Review Logic)")
    print("="*60)
    if manatal_df.empty: return [], pd.DataFrame(), pd.DataFrame()

    # --- Optional date-range filter on submission timestamp ---
    if start_date is not None and 'submitted_utc' in quilgo_df.columns:
        # Default end_date to today when not explicitly provided
        if end_date is None:
            end_date = pd.Timestamp.now(tz='UTC').normalize()

        before_count = len(quilgo_df)
        quilgo_df = quilgo_df[
            quilgo_df['submitted_utc'].notna() &
            (quilgo_df['submitted_utc'] >= start_date) &
            (quilgo_df['submitted_utc'] <= end_date)
        ].copy()
        after_count = len(quilgo_df)
        print(f"  [Date Filter] Kept {after_count} of {before_count} submissions "
              f"between {start_date.date()} and {end_date.date()}.")

    scores_pivot = quilgo_df.pivot_table(index='email', columns='test_name', values='score', aggfunc='first')
    merged_df = manatal_df.merge(scores_pivot, on='email', how='left')
    print(f"  Successfully merged Manatal and Quilgo data for {len(merged_df)} candidates.")

    # Build the set of tests that were actually downloaded/present in this run.
    # Roles whose tests are entirely absent from the download are skipped — they
    # are not reported as FAIL because we simply have no data for them.
    downloaded_tests = set(quilgo_df['test_name'].unique())
    active_roles = {
        role: tests
        for role, tests in ROLE_TO_TEST_MAPPING.items()
        if downloaded_tests.intersection(tests)  # at least one test for this role exists
    }
    if len(active_roles) < len(ROLE_TO_TEST_MAPPING):
        skipped = set(ROLE_TO_TEST_MAPPING) - set(active_roles)
        print(f"  [Role Filter] Skipping {len(skipped)} role(s) with no downloaded tests: {', '.join(skipped)}")
    print(f"  [Role Filter] Evaluating {len(active_roles)} role(s): {', '.join(active_roles)}")

    all_candidates_eval_data = []
    print("\n[LOG] Performing final hierarchical evaluation for all candidates...")
    for _, row in merged_df.iterrows():
        print(f"\n------------------ Evaluating Candidate: {row['full_name']} ({row['email']}) ------------------")
        candidate_eval = {'email': row['email'], 'full_name': row['full_name'], 'original_row': row.to_dict(), 'roles': {}, 'requires_manual_review': False}
        tests_with_scores = [test for test in MASTER_TEST_CONFIG if pd.notna(row.get(test))]
        if not tests_with_scores:
            analyzed_quizzes = sorted([t for t in MASTER_TEST_CONFIG if t in downloaded_tests])
            print("  - No test submissions found for this candidate."); candidate_eval['roles']['No Submission'] = {'status': 'NO SUBMISSION', 'tests': [], 'analyzed_quizzes': analyzed_quizzes}; all_candidates_eval_data.append(candidate_eval); continue
        for role, tests_for_role in active_roles.items():
            print(f"\n--- Evaluating Role: {role} ---")
            role_category = ROLE_TO_CATEGORY_MAPPING.get(role, 'tech')
            role_eval = {'status': 'FAIL', 'tests': [], 'manual_review_reasons': []}
            passing_scores_count = sum(1 for test_name in tests_for_role if pd.notna(row.get(test_name)) and row.get(test_name) >= 7)

            if role_category == 'tech':
                print(f"  [Scoring] Category: tech. Rule: >= 2 scores of 7+. Candidate has: {passing_scores_count}.")
                if passing_scores_count < 2:
                    print(f"  [Scoring] Decision: FAIL.")
                    role_eval['status'] = 'FAIL'
                    for test_name in tests_for_role:
                        if pd.notna(row.get(test_name)): role_eval['tests'].append({'name': test_name, 'score': row.get(test_name), 'status': 'LOGGED'})
                    candidate_eval['roles'][role] = role_eval; print(f"  --> Final Status for {role}: FAIL"); continue
                print(f"  [Scoring] Decision: PASS. Proceeding to integrity check...")
            else:
                # Non-tech: scores are logged but never trigger a fail — integrity is the only disqualifier
                print(f"  [Scoring] Category: non-tech. Scores logged only (no threshold). Passing scores: {passing_scores_count}. Proceeding to integrity check...")
            is_flagged_for_review = False
            for test_name in tests_for_role:
                if pd.notna(row.get(test_name)):
                    score = row.get(test_name)
                    if role_category == 'tech':
                        test_status = "PASS" if score >= 7 else "FAIL"
                    else:
                        # Non-tech: score is informational only — never labelled FAIL
                        test_status = "PASS" if score >= 7 else "LOGGED"
                    if not integrity_df.empty:
                        integrity_issues = integrity_df[(integrity_df['email'] == row['email']) & (integrity_df['test_name'] == test_name)]
                        if not integrity_issues.empty:
                            issue_text = integrity_issues.iloc[0]['Issue_Types']; is_flagged_for_review = True; reason = f"{test_name}: has integrity flags: {issue_text}"
                            role_eval['manual_review_reasons'].append(reason); test_status += " (Integrity Flags)"; print(f"    [Integrity] Flag found for test '{test_name}': {issue_text}")
                    role_eval['tests'].append({'name': test_name, 'score': score, 'status': test_status})
            if not is_flagged_for_review: print("  [Integrity] Decision: No integrity flags found.")
            role_eval['status'] = 'MANUAL REVIEW' if is_flagged_for_review else 'QUALIFIED'
            if is_flagged_for_review: candidate_eval['requires_manual_review'] = True
            candidate_eval['roles'][role] = role_eval; print(f"  --> Final Status for {role}: {role_eval['status']}")
        all_candidates_eval_data.append(candidate_eval)

    # --- FINAL PROCESSING LOOP WITH "SKIP ALL" LOGIC ---
    final_processed_candidates, candidates_for_review, auto_processed_candidates = [], [c for c in all_candidates_eval_data if c.get('requires_manual_review')], [c for c in all_candidates_eval_data if not c.get('requires_manual_review')]

    if not candidates_for_review:
        print("\n✔ NO CANDIDATES FLAGGED FOR MANUAL REVIEW. All candidates were processed automatically.")
    else:
        print(f"\n\n🚨 ATTENTION: {len(candidates_for_review)} CANDIDATE(S) REQUIRE MANUAL REVIEW 🚨")
        
        # --- NEW: Flag to control the main review loop ---
        user_chose_to_skip_all = False
        
        for i, candidate in enumerate(candidates_for_review, 1):
            roles_to_review = {rn: rd for rn, rd in candidate['roles'].items() if rd['status'] == 'MANUAL REVIEW'}
            num_roles_to_review = len(roles_to_review)
            review_count_str = f"({num_roles_to_review} role{'s' if num_roles_to_review > 1 else ''} require review)"
            print(f"\n({i}/{len(candidates_for_review)}) REVIEWING CANDIDATE: {candidate['full_name']} {review_count_str}")
            
            candidate['manual_decisions'] = candidate.get('manual_decisions', [])

            # Helper: mark a role as "Pending" (deferred to the dashboard).
            # This is what 'skip' / 'skip_all' do now — the old behaviour
            # DROPPED the candidate entirely, which meant skipped candidates
            # could silently miss the dashboard. Keeping them with a Pending
            # placeholder lets the reviewer resolve them later.
            def _mark_pending(role_name):
                candidate['roles'][role_name]['status'] = 'MANUAL REVIEW (Pending)'
                candidate['manual_decisions'].append({
                    'role': role_name,
                    'decision': 'Pending',
                    'justification': '',
                })
                print(f"    ⏳ Deferred to dashboard: '{role_name}' marked Pending.")

            deferred_remaining_roles = False

            for j, (role_name, role_data) in enumerate(roles_to_review.items(), 1):
                print(f"  - Reviewing role {j} of {num_roles_to_review}: '{role_name}'")

                if deferred_remaining_roles or user_chose_to_skip_all:
                    # Once we're in deferred mode (skip or skip_all), every
                    # remaining role for this candidate becomes Pending too.
                    _mark_pending(role_name)
                    continue

                decision, justification = get_manual_review_decision(candidate, role_name, j, num_roles_to_review)

                if decision == 'skip_all':
                    print(f"  ⏩ User chose to SKIP ALL remaining reviews. Remaining roles/candidates deferred to dashboard.")
                    user_chose_to_skip_all = True
                    _mark_pending(role_name)
                    continue

                if decision == 'skip':
                    print(f"  ► Skipping remaining reviews for {candidate['full_name']}. Roles deferred to dashboard.")
                    deferred_remaining_roles = True
                    _mark_pending(role_name)
                    continue

                if decision in ['approve', 'reject']:
                    final_decision = "Approved" if decision == 'approve' else "Rejected"
                    candidate['roles'][role_name]['status'] = (
                        f"QUALIFIED (Manually {final_decision})" if decision == 'approve'
                        else f"FAIL (Manually {final_decision})"
                    )
                    candidate['manual_decisions'].append({
                        'role': role_name,
                        'decision': final_decision,
                        'justification': justification,
                    })
                    print(f"    ✔ Decision Logged: Role '{role_name}' was manually {final_decision}.")
                else:
                    # Any unknown / None response is treated as Pending — safe
                    # default: nothing silently slips past manual review.
                    _mark_pending(role_name)

            # Candidate ALWAYS lands on the dashboard, even if every role was
            # deferred. If `user_chose_to_skip_all` is set, the check at the
            # top of the inner loop will auto-defer every role of every
            # remaining candidate too — so they ALL reach the dashboard.
            final_processed_candidates.append(candidate)

    final_processed_candidates.extend(auto_processed_candidates)
    
    # Final data assembly loop is unchanged
    final_rows = []
    for candidate in final_processed_candidates:
        row_dict = candidate['original_row']
        if candidate['roles'].get('No Submission'):
            row_dict['attempt_outcome'] = 'not_attempted'
            analyzed = candidate['roles']['No Submission'].get('analyzed_quizzes', list(MASTER_TEST_CONFIG.keys()))
            quiz_list_md = ', '.join(analyzed) if analyzed else 'N/A'
            quiz_list_html = ', '.join(analyzed) if analyzed else 'N/A'
            row_dict['summary_note_md'] = f"**Status:** No Submission\n\n**Quizzes Analyzed:** {quiz_list_md}\n\n**Recommendation:** Candidate did not submit any of the assessed quizzes."
            row_dict['summary_note_html'] = f"<p><strong>Status:</strong> No Submission</p><p><strong>Quizzes Analyzed:</strong> {quiz_list_html}</p><p><strong>Recommendation:</strong> Candidate did not submit any of the assessed quizzes.</p>"
            row_dict['scores_to_update'] = json.dumps({'techtestspassed': ['FAIL: No Submission']})
        else:
            final_note_md, final_note_html = _generate_summary_notes(candidate, integrity_df, candidate.get('manual_decisions'))
            row_dict['summary_note_md'] = final_note_md
            row_dict['summary_note_html'] = final_note_html
            qualified_roles = [role for role, data in candidate['roles'].items() if "QUALIFIED" in data.get('status', 'FAIL')]
            scores_payload = {slug: row_dict.get(test) for test, slug in SLUG_MAPPING.items() if pd.notna(row_dict.get(test))}
            scores_payload['techtestspassed'] = [ROLE_TO_DROPDOWN_OPTION_MAP.get(r, r) for r in qualified_roles] if qualified_roles else ["FAIL: Did not meet minimum requirements"]
            row_dict['scores_to_update'] = json.dumps(scores_payload)
            # Outcome used by api_pusher to select the correct Manatal stage transition
            row_dict['attempt_outcome'] = 'passed' if qualified_roles else 'attempted_failed'
        final_rows.append(row_dict)

    if not final_rows: return [], pd.DataFrame(), pd.DataFrame()
    final_df = pd.DataFrame(final_rows)
    final_approved_df = final_df[~final_df['scores_to_update'].str.contains("FAIL", na=False)].copy()
    final_rejected_df = final_df[final_df['scores_to_update'].str.contains("FAIL", na=False)].copy()
    
    print(f"\n✔ Evaluation and triage complete. {len(final_approved_df)} candidates approved. {len(final_rejected_df)} candidates rejected.")
    return final_processed_candidates, final_approved_df, final_rejected_df