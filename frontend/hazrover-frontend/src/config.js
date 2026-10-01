export const API = import.meta.env.VITE_API_URL || "https://tapes-dairy-antiques-peninsula.trycloudflare.com";
export const CONTRACT = import.meta.env.VITE_CONTRACT_ADDRESS || "0xcf940493a753c5980f50f80f76723b7478607153";
export const CHAIN = { id: 91562037, hex: "0x5752035", rpc: "https://testnetrpc.mstblockchain.com", scan: "https://testnet.mstscan.com" };

export const ABI = [
  "function depositEscrow() payable",
  "function setRover(address rover)",
  "function facilities(address) view returns (uint256 escrow, uint256 lastHeartbeatTimestamp, address rover, bool registered, uint256 totalSlashed)",
  "function isHeartbeatExpired(address) view returns (bool)",
  "function heartbeatTimeout() view returns (uint256)",
  "function gasPenalty() view returns (uint256)",
  "function sabotagePenalty() view returns (uint256)",
  "function timeoutPenalty() view returns (uint256)",
  "function treasury() view returns (address)",
];

// Copied from ComplianceVault.sol
export const EVENTS = [
  "event EscrowDeposited(address indexed facility, uint256 amount)",
  "event RoverSet(address indexed facility, address indexed rover)",
  "event Heartbeat(address indexed facility, uint256 timestamp)",
  "event Slashed(address indexed facility, uint256 amount, string reason, bytes32 evidence)",
  "event ParamsUpdated()",
];