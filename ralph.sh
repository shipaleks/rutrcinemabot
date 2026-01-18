#!/bin/bash
# =============================================================================
# Ralph Wiggum Runner for Media Concierge Bot
# =============================================================================
# Usage: ./ralph.sh [options]
#
# Options:
#   --max-iterations N    Maximum iterations (default: 50)
#   --timeout MINUTES     Timeout per iteration in minutes (default: 10)
#   --model MODEL         Claude model to use (default: opus)
#   --pause-hours N       Hours to pause when hitting quota (default: 4)
#   --dry-run             Show what would be run without executing
#   --help                Show this help message
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

# Default configuration
MAX_ITERATIONS=50
TIMEOUT_MINUTES=10
MODEL="opus"
PAUSE_HOURS=4
DRY_RUN=false
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="${PROJECT_DIR}/ralph.log"
PROMPT_FILE="${PROJECT_DIR}/PROMPT.md"
QUOTA_ERRORS=0
MAX_QUOTA_RETRIES=3

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --max-iterations)
            MAX_ITERATIONS="$2"
            shift 2
            ;;
        --timeout)
            TIMEOUT_MINUTES="$2"
            shift 2
            ;;
        --model)
            MODEL="$2"
            shift 2
            ;;
        --pause-hours)
            PAUSE_HOURS="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help)
            head -20 "$0" | tail -15
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            exit 1
            ;;
    esac
done

# Logging function
log() {
    local level=$1
    shift
    local message="$*"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo -e "[${timestamp}] [${level}] ${message}" | tee -a "$LOG_FILE"
}

# Check prerequisites
check_prerequisites() {
    log "INFO" "Checking prerequisites..."
    
    # Check Claude Code CLI
    if ! command -v claude &> /dev/null; then
        log "ERROR" "${RED}Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code${NC}"
        exit 1
    fi
    
    # Check git
    if ! command -v git &> /dev/null; then
        log "ERROR" "${RED}Git not found${NC}"
        exit 1
    fi
    
    # Check project files
    if [[ ! -f "${PROJECT_DIR}/prd.json" ]]; then
        log "ERROR" "${RED}prd.json not found in ${PROJECT_DIR}${NC}"
        exit 1
    fi
    
    if [[ ! -f "${PROMPT_FILE}" ]]; then
        log "ERROR" "${RED}PROMPT.md not found in ${PROJECT_DIR}${NC}"
        exit 1
    fi
    
    log "INFO" "${GREEN}All prerequisites met${NC}"
}

# Initialize git if needed
init_git() {
    if [[ ! -d "${PROJECT_DIR}/.git" ]]; then
        log "INFO" "Initializing git repository..."
        cd "$PROJECT_DIR"
        git init
        git add -A
        git commit -m "chore: initial commit from Ralph"
    fi
}

# Count remaining tasks
count_remaining_tasks() {
    python3 -c "
import json
with open('${PROJECT_DIR}/prd.json') as f:
    prd = json.load(f)
remaining = sum(1 for s in prd['userStories'] if not s.get('passes', False))
print(remaining)
"
}

# Get next task ID
get_next_task() {
    python3 -c "
import json
with open('${PROJECT_DIR}/prd.json') as f:
    prd = json.load(f)
for story in sorted(prd['userStories'], key=lambda x: x['priority']):
    if not story.get('passes', False):
        print(story['id'])
        break
"
}

# Main Ralph loop
run_ralph() {
    local iteration=1
    local start_time=$(date +%s)
    
    log "INFO" "${BLUE}Starting Ralph Wiggum loop${NC}"
    log "INFO" "Max iterations: ${MAX_ITERATIONS}"
    log "INFO" "Timeout per iteration: ${TIMEOUT_MINUTES} minutes"
    log "INFO" "Model: ${MODEL}"
    log "INFO" "Pause on quota: ${PAUSE_HOURS} hours"
    
    while [[ $iteration -le $MAX_ITERATIONS ]]; do
        local remaining=$(count_remaining_tasks)
        local next_task=$(get_next_task)
        
        log "INFO" "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        log "INFO" "${YELLOW}Iteration ${iteration}/${MAX_ITERATIONS} | Remaining tasks: ${remaining} | Next: ${next_task}${NC}"
        log "INFO" "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        
        # Check if all tasks complete
        if [[ $remaining -eq 0 ]]; then
            log "INFO" "${GREEN}🎉 All tasks complete! Ralph is done.${NC}"
            break
        fi
        
        if [[ "$DRY_RUN" == "true" ]]; then
            log "INFO" "[DRY RUN] Would execute Claude Code with PROMPT.md"
            break
        fi
        
        # Build the prompt
        local prompt="@PROMPT.md @prd.json @progress.txt

Read the files above carefully. You are in iteration ${iteration} of the Ralph loop.

Current state:
- Remaining tasks: ${remaining}
- Next task to work on: ${next_task}

IMPORTANT:
1. ONLY work on task ${next_task}
2. Follow the acceptance criteria exactly
3. Run verification steps before marking complete
4. Update prd.json when task passes
5. Log learnings in progress.txt
6. Commit your changes with a descriptive message

If ALL tasks in prd.json have passes: true, output:
<promise>COMPLETE</promise>

If you're stuck on ${next_task} for more than 5 attempts, document the blocker and output:
<promise>BLOCKED</promise>

Now begin working on ${next_task}."

        # Run Claude Code
        local iter_start=$(date +%s)
        
        cd "$PROJECT_DIR"
        
        # Run with timeout and capture output
        local output
        local exit_code=0
        output=$(timeout "${TIMEOUT_MINUTES}m" claude \
            --model "$MODEL" \
            --permission-mode bypassPermissions \
            --allowedTools "Bash Edit Write Read Glob Grep TodoWrite" \
            --output-format json \
            -p "$prompt" 2>&1) || exit_code=$?
        
        local iter_end=$(date +%s)
        local iter_duration=$((iter_end - iter_start))
        
        # Check for quota/rate limit errors
        if echo "$output" | grep -qiE "rate.?limit|quota|too many requests|429|capacity|overloaded"; then
            QUOTA_ERRORS=$((QUOTA_ERRORS + 1))
            log "WARN" "${MAGENTA}⏸️  Quota/rate limit hit (attempt ${QUOTA_ERRORS}/${MAX_QUOTA_RETRIES})${NC}"
            
            if [[ $QUOTA_ERRORS -ge $MAX_QUOTA_RETRIES ]]; then
                local pause_seconds=$((PAUSE_HOURS * 3600))
                local resume_time=$(date -d "+${PAUSE_HOURS} hours" '+%H:%M' 2>/dev/null || date -v+${PAUSE_HOURS}H '+%H:%M')
                
                log "INFO" "${MAGENTA}😴 Pausing for ${PAUSE_HOURS} hours (until ~${resume_time})...${NC}"
                log "INFO" "${MAGENTA}   You can Ctrl+C to stop, then resume later with: ./ralph.sh${NC}"
                
                # Save state before sleeping
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] Paused due to quota. Iteration: ${iteration}, Task: ${next_task}" >> progress.txt
                
                sleep $pause_seconds
                
                QUOTA_ERRORS=0
                log "INFO" "${GREEN}⏰ Resuming after quota pause...${NC}"
                continue
            else
                # Short pause before retry
                log "INFO" "Waiting 60 seconds before retry..."
                sleep 60
                continue
            fi
        fi
        
        # Reset quota error counter on success
        QUOTA_ERRORS=0
        
        if [[ $exit_code -eq 0 ]]; then
            log "INFO" "Iteration completed in ${iter_duration}s"
            
            # Check for completion promise
            if echo "$output" | grep -q "<promise>COMPLETE</promise>"; then
                log "INFO" "${GREEN}🎉 COMPLETE signal received! All tasks done.${NC}"
                break
            fi
            
            # Check for blocked promise
            if echo "$output" | grep -q "<promise>BLOCKED</promise>"; then
                log "WARN" "${YELLOW}⚠️ BLOCKED signal received. Check progress.txt for details.${NC}"
            fi
            
        else
            if [[ $exit_code -eq 124 ]]; then
                log "WARN" "${YELLOW}Iteration timed out after ${TIMEOUT_MINUTES} minutes${NC}"
            else
                log "ERROR" "${RED}Claude Code exited with code ${exit_code}${NC}"
                # Log error details
                echo "$output" | tail -20 >> "$LOG_FILE"
            fi
        fi
        
        # Brief pause between iterations
        sleep 2
        
        ((iteration++))
    done
    
    local end_time=$(date +%s)
    local total_duration=$((end_time - start_time))
    local minutes=$((total_duration / 60))
    local seconds=$((total_duration % 60))
    
    log "INFO" "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    log "INFO" "${BLUE}Ralph session complete${NC}"
    log "INFO" "Total iterations: $((iteration - 1))"
    log "INFO" "Total time: ${minutes}m ${seconds}s"
    log "INFO" "Remaining tasks: $(count_remaining_tasks)"
    log "INFO" "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# Status command
show_status() {
    echo -e "${BLUE}Media Concierge Bot - Ralph Status${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    python3 << 'EOF'
import json

with open('prd.json') as f:
    prd = json.load(f)

stories = prd['userStories']
total = len(stories)
passed = sum(1 for s in stories if s.get('passes', False))
remaining = total - passed

print(f"Total tasks: {total}")
print(f"Completed:   {passed} ✅")
print(f"Remaining:   {remaining} ⏳")
print()
print("Next tasks to complete:")
for story in sorted(stories, key=lambda x: x['priority']):
    if not story.get('passes', False):
        print(f"  [{story['id']}] {story['title']}")
        if sum(1 for s in stories if not s.get('passes', False) and s['priority'] <= story['priority']) >= 5:
            break
EOF
}

# Main
main() {
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║                     🔄 Ralph Wiggum Runner                    ║"
    echo "║              Media Concierge Bot Development                  ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    
    check_prerequisites
    init_git
    show_status
    echo ""
    
    read -p "Start Ralph loop? (y/N) " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        run_ralph
    else
        log "INFO" "Aborted by user"
    fi
}

main "$@"
