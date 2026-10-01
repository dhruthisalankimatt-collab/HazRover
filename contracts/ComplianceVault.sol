// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title ComplianceVault - HazRover escrow + deterministic slashing
/// @notice Slashing is pure math / timers. No AI, no oracle opinions.
contract ComplianceVault {
    struct Facility {
        uint256 escrow;
        uint256 lastHeartbeatTimestamp;
        address rover;
        bool registered;
        uint256 totalSlashed;
    }

    // reason codes emitted in Slashed()
    uint8 public constant REASON_GAS = 0;
    uint8 public constant REASON_SABOTAGE = 1;
    uint8 public constant REASON_HEARTBEAT = 2;

    uint256 public constant GAS_THRESHOLD_PPM = 1800; // slash only if gasPpm > 1800

    address public immutable owner;
    address public operator;   // backend wallet allowed to submit evidence
    address public treasury;   // receives slashed funds

    uint256 public immutable heartbeatTimeout; // 15 minutes in production
    uint256 public gasPenalty;
    uint256 public sabotagePenalty;            // 50_000 ether in the spec
    uint256 public heartbeatPenalty;

    mapping(address => Facility) public facilities;
    mapping(bytes32 => bool) public evidenceUsed; // replay protection

    event EscrowDeposited(address indexed facility, address indexed rover, uint256 amount);
    event HeartbeatReceived(address indexed facility, uint256 timestamp);
    event Slashed(address indexed facility, uint8 reason, uint256 amount, bytes32 evidenceHash);
    event OperatorChanged(address indexed newOperator);

    modifier onlyOwner() { require(msg.sender == owner, "not owner"); _; }
    modifier onlyOperator() { require(msg.sender == operator, "not operator"); _; }

    constructor(
        address _operator,
        address _treasury,
        uint256 _gasPenalty,
        uint256 _sabotagePenalty,
        uint256 _heartbeatPenalty,
        uint256 _heartbeatTimeout
    ) {
        require(_operator != address(0) && _treasury != address(0), "zero addr");
        owner = msg.sender;
        operator = _operator;
        treasury = _treasury;
        gasPenalty = _gasPenalty;
        sabotagePenalty = _sabotagePenalty;
        heartbeatPenalty = _heartbeatPenalty;
        heartbeatTimeout = _heartbeatTimeout;
    }

    function setOperator(address _operator) external onlyOwner {
        require(_operator != address(0), "zero addr");
        operator = _operator;
        emit OperatorChanged(_operator);
    }

    /// @notice Factory owner locks escrow. `rover` is the rover's identifier address.
    function depositEscrow(address rover) external payable {
        require(msg.value > 0, "no value");
        Facility storage f = facilities[msg.sender];
        if (!f.registered) {
            f.registered = true;
            f.lastHeartbeatTimestamp = block.timestamp;
        }
        if (rover != address(0)) f.rover = rover;
        f.escrow += msg.value;
        emit EscrowDeposited(msg.sender, f.rover, msg.value);
    }

    /// @notice Relayed by the backend every ~5 minutes.
    function heartbeat(address facility) external onlyOperator {
        require(facilities[facility].registered, "not registered");
        facilities[facility].lastHeartbeatTimestamp = block.timestamp;
        emit HeartbeatReceived(facility, block.timestamp);
    }

    /// @notice Hard-math gas violation. Reverts unless gasPpm > 1800.
    function slashEscrow(address facility, uint256 gasPpm, bytes32 evidenceHash) external onlyOperator {
        require(gasPpm > GAS_THRESHOLD_PPM, "below threshold");
        _consumeEvidence(evidenceHash);
        _slash(facility, gasPenalty, REASON_GAS, evidenceHash);
    }

    /// @notice Tamper / kick / flip detected by the IMU.
    function slashForSabotage(address facility, bytes32 proofHash) external onlyOperator {
        _consumeEvidence(proofHash);
        _slash(facility, sabotagePenalty, REASON_SABOTAGE, proofHash);
    }

    function isHeartbeatExpired(address facility) public view returns (bool) {
        Facility storage f = facilities[facility];
        return f.registered && f.escrow > 0 &&
            block.timestamp > f.lastHeartbeatTimestamp + heartbeatTimeout;
    }

    /// @notice Anyone can trigger it - that's the deadman's switch.
    function checkHeartbeat(address facility) external {
        require(isHeartbeatExpired(facility), "heartbeat ok");
        facilities[facility].lastHeartbeatTimestamp = block.timestamp; // next slash needs another full timeout
        bytes32 ev = keccak256(abi.encode(facility, block.timestamp));
        _slash(facility, heartbeatPenalty, REASON_HEARTBEAT, ev);
    }

    // ---------- internal ----------
    function _consumeEvidence(bytes32 h) internal {
        require(!evidenceUsed[h], "evidence replayed");
        evidenceUsed[h] = true;
    }

    function _slash(address facility, uint256 amount, uint8 reason, bytes32 evidence) internal {
        Facility storage f = facilities[facility];
        require(f.registered, "not registered");
        uint256 pay = amount > f.escrow ? f.escrow : amount; // cap at remaining escrow
        require(pay > 0, "empty escrow");
        f.escrow -= pay;          // effects before interaction
        f.totalSlashed += pay;
        (bool ok, ) = treasury.call{value: pay}("");
        require(ok, "treasury transfer failed");
        emit Slashed(facility, reason, pay, evidence);
    }
}
