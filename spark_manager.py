import os
import re
import sys

from releaseutils import (
    tag_exists,
    get_commits,
    yesOrNoPrompt,
    get_date,
    is_valid_author,
    capitalize_author,
    JIRA,
    find_components,
    translate_issue_type,
    translate_component,
    CORE_COMPONENT,
    contributors_file_name,
    nice_join,
)

# You must set the following before use!
JIRA_API_BASE = os.environ.get("JIRA_API_BASE", "https://issues.apache.org/jira")
RELEASE_TAG = os.environ.get("RELEASE_TAG", "v1.2.0-rc2")
PREVIOUS_RELEASE_TAG = os.environ.get("PREVIOUS_RELEASE_TAG", "v1.1.0")

# If the release tags are not provided, prompt the user to provide them
while not tag_exists(RELEASE_TAG):
    RELEASE_TAG = input("Please provide a valid release tag: ")
while not tag_exists(PREVIOUS_RELEASE_TAG):
    PREVIOUS_RELEASE_TAG = input("Please specify the previous release tag: ")

# Gather commits found in the new tag but not in the old tag.
print(f"Gathering new commits between tags {PREVIOUS_RELEASE_TAG} and {RELEASE_TAG}")
release_commits = get_commits(RELEASE_TAG)
previous_release_commits = get_commits(PREVIOUS_RELEASE_TAG)

# Extract hashes and PR numbers from previous release commits
previous_release_hashes = {commit.get_hash() for commit in previous_release_commits}
previous_release_prs = {commit.get_pr_number() for commit in previous_release_commits if commit.get_pr_number()}

# Filter out new commits that are not in the previous release
new_commits = [
    commit for commit in release_commits
    if commit.get_hash() not in previous_release_hashes and
    (commit.get_pr_number() not in previous_release_prs if commit.get_pr_number() else True)
]

if not new_commits:
    sys.exit(f"There are no new commits between {PREVIOUS_RELEASE_TAG} and {RELEASE_TAG}!")

# Prompt the user for confirmation
print("\n==================================================================================")
print(f"JIRA server: {JIRA_API_BASE}")
print(f"Release tag: {RELEASE_TAG}")
print(f"Previous release tag: {PREVIOUS_RELEASE_TAG}")
print(f"Number of commits in this range: {len(new_commits)}")
print("")

if yesOrNoPrompt("Show all commits?"):
    for commit in new_commits:
        print(f"  {commit.get_title()}")
print("==================================================================================\n")
if not yesOrNoPrompt("Does this look correct?"):
    sys.exit("Ok, exiting")

# Initialize lists for special commits
releases = []
maintenance = []
reverts = []
nojiras = []
filtered_commits = []

def is_release(commit_title):
    return any(phrase in commit_title.lower() for phrase in ["[release]", "preparing spark release", "preparing development version", "changes.txt"])

def is_maintenance(commit_title):
    return any(phrase in commit_title.lower() for phrase in ["maintenance", "manually close"])

def has_no_jira(commit_title):
    return not re.search("SPARK-[0-9]+", commit_title.upper())

def is_revert(commit_title):
    return "revert" in commit_title.lower()

def is_docs(commit_title):
    return "docs" in commit_title.lower() or "programming guide" in commit_title.lower()

# Classify commits based on title
for commit in new_commits:
    title = commit.get_title() or ""
    if is_release(title):
        releases.append(commit)
    elif is_maintenance(title):
        maintenance.append(commit)
    elif is_revert(title):
        reverts.append(commit)
    elif is_docs(title):
        filtered_commits.append(commit)  # docs may not have JIRA numbers
    elif has_no_jira(title):
        nojiras.append(commit)
    else:
        filtered_commits.append(commit)

# Warn against ignored commits
if releases or maintenance or reverts or nojiras:
    print("\n==================================================================================")
    if releases:
        print(f"Found {len(releases)} release commits")
    if maintenance:
        print(f"Found {len(maintenance)} maintenance commits")
    if reverts:
        print(f"Found {len(reverts)} revert commits")
    if nojiras:
        print(f"Found {len(nojiras)} commits with no JIRA")
    print("* Warning: these commits will be ignored.\n")
    if yesOrNoPrompt("Show ignored commits?"):
        if releases:
            print("Release (%d)" % len(releases))
            for commit in releases:
                print(f"  {commit.get_title()}")
        if maintenance:
            print("Maintenance (%d)" % len(maintenance))
            for commit in maintenance:
                print(f"  {commit.get_title()}")
        if reverts:
            print("Revert (%d)" % len(reverts))
            for commit in reverts:
                print(f"  {commit.get_title()}")
        if nojiras:
            print("No JIRA (%d)" % len(nojiras))
            for commit in nojiras:
                print(f"  {commit.get_title()}")
    print("==================== Warning: the above commits will be ignored ==================\n")
prompt_msg = f"{len(filtered_commits)} commits left to process after filtering. Ok to proceed?"
if not yesOrNoPrompt(prompt_msg):
    sys.exit("Ok, exiting.")

# Keep track of warnings and invalid authors
warnings = []
invalid_authors = {}

# Populate a map that groups issues and components by author
author_info = {}
jira_options = {"server": JIRA_API_BASE}
jira_client = JIRA(options=jira_options)
print("\n=========================== Compiling contributor list ===========================")
for commit in filtered_commits:
    _hash = commit.get_hash()
    title = commit.get_title()
    issues = re.findall("SPARK-[0-9]+", title.upper())
    author = commit.get_author()
    date = get_date(_hash)
    if not is_valid_author(author):
        if author not in invalid_authors:
            invalid_authors[author] = set()
        for issue in issues:
            invalid_authors[author].add(issue)
        author = f"{author}/{'/'.join(invalid_authors[author])}"

    commit_components = find_components(title, _hash)
    
    def populate(issue_type, components):
        components = components or [CORE_COMPONENT]
        if author not in author_info:
            author_info[author] = {}
        if issue_type not in author_info[author]:
            author_info[author][issue_type] = set()
        author_info[author][issue_type].update(components)

    for issue in issues:
        try:
            jira_issue = jira_client.issue(issue)
            jira_type = jira_issue.fields.issuetype.name
            jira_type = translate_issue_type(jira_type, issue, warnings)
            jira_components = [translate_component(c.name, _hash, warnings) for c in jira_issue.fields.components]
            all_components = set(jira_components + commit_components)
            populate(jira_type, all_components)
        except Exception as e:
            print(f"Unexpected error: {e}")
    if is_docs(title) and not issues:
        populate("documentation", commit_components)
    print(f"  Processed commit {_hash} authored by {author} on {date}")
print("==================================================================================\n")

# Write to contributors file
with open(contributors_file_name, "w") as contributors_file:
    authors = sorted(author_info.keys())
    for author in authors:
        contribution = ""
        components = set()
        issue_types = set()
        for issue_type, comps in author_info[author].items():
            components.update(comps)
            issue_types.add(issue_type)
        if len(components) == 1:
            contribution = f"{nice_join(issue_types)} in {next(iter(components))}"
        else:
            contributions = [f"{issue_type} in {nice_join(comps)}" for issue_type, comps in author_info[author].items()]
            contribution = "; ".join(contributions)
        contribution = contribution[0].capitalize() + contribution[1:]
        line = f"{author} -- {contribution}"
        contributors_file.write(line + "\n")
print(f"Contributors list is successfully written to {contributors_file_name}!")

# Prompt the user to translate author names if necessary
if invalid_authors:
    warnings.append("Found the following invalid authors:")
    for author in invalid_authors:
        warnings.append(f"\t{author}")
    warnings.append("Please run './translate-contributors.py' to translate them.")

# Log any warnings encountered
if warnings:
    print("\n============ Warnings encountered while creating the contributor list ============")
    for warning in warnings:
        print(warning)
    print(f"Please correct these in the final contributors list at {contributors_file_name}.")
    print("==================================================================================\n")
