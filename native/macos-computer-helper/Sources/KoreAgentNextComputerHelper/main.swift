import Foundation
import Darwin
import KoreAgentNextComputerCore

let decoder = JSONDecoder(), encoder = JSONEncoder(), controller = ComputerController()
encoder.outputFormatting = [.sortedKeys]

while let line = readLine() {
    guard let data = line.data(using: .utf8) else { continue }
    let response: HelperResponse
    do {
        let request = try decoder.decode(HelperRequest.self, from: data)
        do { response = HelperResponse(id: request.id, result: try controller.handle(request)) }
        catch let error as HelperError { response = HelperResponse(id: request.id, error: error) }
        catch { response = HelperResponse(id: request.id, error: HelperError("internal_error", error.localizedDescription)) }
    } catch { response = HelperResponse(id: .null, error: HelperError("invalid_request", error.localizedDescription)) }
    if let output = try? encoder.encode(response), let text = String(data: output, encoding: .utf8) {
        print(text); fflush(stdout)
    }
}

