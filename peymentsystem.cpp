
#include <iostream>
#include <memory>
#include <string>
#include <cstdlib>
#include <ctime>

using namespace std;

// Payment gateway types
enum class GatewayType {
    PAYTM,
    RAZORPAY
};

// Payment request details
struct PaymentRequest {
    string sender;
    string receiver;
    double amount;
    string currency;

    PaymentRequest(const string& sender,
                   const string& receiver,
                   double amount,
                   const string& currency)
        : sender(sender),
          receiver(receiver),
          amount(amount),
          currency(currency) {}
};

// Banking system interface
class BankingSystem {
public:
    virtual bool processPayment(double amount) = 0;
    virtual ~BankingSystem() = default;
};

// Paytm banking simulation
class PaytmBankingSystem : public BankingSystem {
public:
    bool processPayment(double amount) override {
        cout << "[Paytm Banking] Processing INR "
             << amount << "...\n";

        return (rand() % 100) < 80;
    }
};

// Razorpay banking simulation
class RazorpayBankingSystem : public BankingSystem {
public:
    bool processPayment(double amount) override {
        cout << "[Razorpay Banking] Processing INR "
             << amount << "...\n";

        return (rand() % 100) < 95;
    }
};

// Abstract payment gateway
class PaymentGateway {
protected:
    unique_ptr<BankingSystem> bankingSystem;

public:
    PaymentGateway() = default;
    virtual ~PaymentGateway() = default;

    // Template Method
    virtual bool processPayment(PaymentRequest* request) {
        if (request == nullptr) {
            cout << "[PaymentGateway] Invalid request.\n";
            return false;
        }

        if (!validatePayment(request)) {
            cout << "[PaymentGateway] Validation failed.\n";
            return false;
        }

        if (!initiatePayment(request)) {
            cout << "[PaymentGateway] Payment failed.\n";
            return false;
        }

        if (!confirmPayment(request)) {
            cout << "[PaymentGateway] Confirmation failed.\n";
            return false;
        }

        cout << "[PaymentGateway] Payment completed.\n";
        return true;
    }

    virtual bool validatePayment(PaymentRequest* request) = 0;
    virtual bool initiatePayment(PaymentRequest* request) = 0;
    virtual bool confirmPayment(PaymentRequest* request) = 0;
};

// Paytm gateway
class PaytmGateway : public PaymentGateway {
public:
    PaytmGateway() {
        bankingSystem = make_unique<PaytmBankingSystem>();
    }

    bool validatePayment(PaymentRequest* request) override {
        cout << "[Paytm] Validating payment...\n";

        return request != nullptr &&
               !request->sender.empty() &&
               !request->receiver.empty() &&
               request->amount > 0 &&
               request->currency == "INR";
    }

    bool initiatePayment(PaymentRequest* request) override {
        cout << "[Paytm] Initiating payment of "
             << request->amount << " "
             << request->currency << " for "
             << request->sender << ".\n";

        return bankingSystem != nullptr &&
               bankingSystem->processPayment(request->amount);
    }

    bool confirmPayment(PaymentRequest* request) override {
        cout << "[Paytm] Confirming payment for "
             << request->sender << "...\n";

        // Simulation only: no real gateway confirmation.
        return true;
    }
};

// Razorpay gateway
class RazorpayGateway : public PaymentGateway {
public:
    RazorpayGateway() {
        bankingSystem = make_unique<RazorpayBankingSystem>();
    }

    bool validatePayment(PaymentRequest* request) override {
        cout << "[Razorpay] Validating payment...\n";

        return request != nullptr &&
               !request->sender.empty() &&
               !request->receiver.empty() &&
               request->amount > 0 &&
               request->currency == "INR";
    }

    bool initiatePayment(PaymentRequest* request) override {
        cout << "[Razorpay] Initiating payment of "
             << request->amount << " "
             << request->currency << " for "
             << request->sender << ".\n";

        return bankingSystem != nullptr &&
               bankingSystem->processPayment(request->amount);
    }

    bool confirmPayment(PaymentRequest* request) override {
        cout << "[Razorpay] Confirming payment for "
             << request->sender << "...\n";

        // Simulation only: no real gateway confirmation.
        return true;
    }
};

// Proxy pattern: retry failed simulated payments
class PaymentGatewayProxy : public PaymentGateway {
private:
    unique_ptr<PaymentGateway> realGateway;
    int maxRetries;

public:
    PaymentGatewayProxy(unique_ptr<PaymentGateway> gateway,
                        int retries)
        : realGateway(std::move(gateway)),
          maxRetries(retries) {}

    bool processPayment(PaymentRequest* request) override {
        if (realGateway == nullptr || request == nullptr) {
            cout << "[Proxy] Invalid gateway or request.\n";
            return false;
        }

        for (int attempt = 1; attempt <= maxRetries; ++attempt) {
            if (attempt > 1) {
                cout << "[Proxy] Retrying payment, attempt "
                     << attempt << "...\n";
            }

            if (realGateway->processPayment(request)) {
                return true;
            }

            cout << "[Proxy] Attempt "
                 << attempt << " failed.\n";
        }

        return false;
    }

    bool validatePayment(PaymentRequest* request) override {
        return realGateway != nullptr &&
               realGateway->validatePayment(request);
    }

    bool initiatePayment(PaymentRequest* request) override {
        return realGateway != nullptr &&
               realGateway->initiatePayment(request);
    }

    bool confirmPayment(PaymentRequest* request) override {
        return realGateway != nullptr &&
               realGateway->confirmPayment(request);
    }
};

// Singleton Gateway Factory
class GatewayFactory {
private:
    GatewayFactory() = default;

    GatewayFactory(const GatewayFactory&) = delete;
    GatewayFactory& operator=(const GatewayFactory&) = delete;

public:
    static GatewayFactory& getInstance() {
        static GatewayFactory instance;
        return instance;
    }

    unique_ptr<PaymentGateway> getGateway(GatewayType type) {
        unique_ptr<PaymentGateway> gateway;

        switch (type) {
            case GatewayType::PAYTM:
                gateway = make_unique<PaytmGateway>();
                break;

            case GatewayType::RAZORPAY:
                gateway = make_unique<RazorpayGateway>();
                break;

            default:
                cout << "[Factory] Invalid gateway type.\n";
                return nullptr;
        }

        return make_unique<PaymentGatewayProxy>(std::move(gateway), 3);
    }
};

// Singleton Payment Service
class PaymentService {
private:
    unique_ptr<PaymentGateway> gateway;

    PaymentService() = default;

    PaymentService(const PaymentService&) = delete;
    PaymentService& operator=(const PaymentService&) = delete;

public:
    static PaymentService& getInstance() {
        static PaymentService instance;
        return instance;
    }

    void setGateway(unique_ptr<PaymentGateway> newGateway) {
        gateway = std::move(newGateway);
    }

    bool processPayment(PaymentRequest* request) {
        if (gateway == nullptr) {
            cout << "[PaymentService] No gateway selected.\n";
            return false;
        }

        return gateway->processPayment(request);
    }
};

// Singleton Payment Controller
class PaymentController {
private:
    PaymentController() = default;

    PaymentController(const PaymentController&) = delete;
    PaymentController& operator=(const PaymentController&) = delete;

public:
    static PaymentController& getInstance() {
        static PaymentController instance;
        return instance;
    }

    bool handlePayment(GatewayType type, PaymentRequest* request) {
        auto gateway = GatewayFactory::getInstance().getGateway(type);

        if (gateway == nullptr) {
            cout << "[Controller] Invalid gateway type.\n";
            return false;
        }

        PaymentService::getInstance().setGateway(std::move(gateway));
        return PaymentService::getInstance().processPayment(request);
    }
};

// Main function
int main() {
    srand(static_cast<unsigned int>(time(nullptr)));

    PaymentRequest request(
        "Alice",
        "Bob",
        1500.0,
        "INR"
    );

    cout << "===== PAYMENT SYSTEM =====\n";
    cout << "Sender   : " << request.sender << "\n";
    cout << "Receiver : " << request.receiver << "\n";
    cout << "Amount   : " << request.amount << " "
         << request.currency << "\n\n";

    // Choose PAYTM or RAZORPAY
    GatewayType selectedGateway = GatewayType::PAYTM;

    bool success =
        PaymentController::getInstance()->handlePayment(
            selectedGateway, &request
        );

    if (success) {
        cout << "\n[Main] Payment Successful!\n";
    } else {
        cout << "\n[Main] Payment Failed!\n";
    }

    return 0;
}
